"""Tests for the resubmit_error_certificates management command. """


from unittest.mock import patch

import ddt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.tests.factories import CourseEnrollmentFactory, UserFactory
from lms.djangoapps.badges.events.course_complete import get_completion_badge
from lms.djangoapps.badges.models import BadgeAssertion
from lms.djangoapps.badges.tests.factories import BadgeAssertionFactory, CourseCompleteImageConfigurationFactory
from lms.djangoapps.certificates.data import CertificateStatuses
from lms.djangoapps.certificates.models import GeneratedCertificate
from lms.djangoapps.certificates.tests.factories import GeneratedCertificateFactory
from lms.djangoapps.grades.tests.utils import mock_passing_grade
from openedx.core.djangoapps.content.course_overviews.tests.factories import CourseOverviewFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory


class CertificateManagementTest(ModuleStoreTestCase):
    """
    Base test class for Certificate Management command tests.
    """
    # Override with the command module you wish to test.
    command = 'resubmit_error_certificates'

    def setUp(self):
        super().setUp()
        self.user = UserFactory.create()
        self.courses = [
            CourseFactory.create()
            for __ in range(3)
        ]
        for course in self.courses:
            chapter = ItemFactory.create(parent_location=course.location)
            ItemFactory.create(parent_location=chapter.location, category='sequential', graded=True)
        CourseCompleteImageConfigurationFactory.create()

    def _create_cert(self, course_key, user, status, mode=CourseMode.HONOR):
        """Create a certificate entry. """
        # Enroll the user in the course
        CourseEnrollmentFactory.create(
            user=user,
            course_id=course_key,
            mode=mode
        )

        # Create the certificate
        GeneratedCertificateFactory(
            user=user,
            course_id=course_key,
            status=status
        )

    def _assert_cert_status(self, course_key, user, expected_status):
        """Check the status of a certificate. """
        cert = GeneratedCertificate.eligible_certificates.get(user=user, course_id=course_key)
        assert cert.status == expected_status


@ddt.ddt
class ResubmitErrorCertificatesTest(CertificateManagementTest):
    """Tests for the resubmit_error_certificates management command. """
    ENABLED_SIGNALS = ['course_published']

    @ddt.data(CourseMode.HONOR, CourseMode.VERIFIED)
    def test_resubmit_error_certificate(self, mode):
        # Create a certificate with status 'error'
        self._create_cert(self.courses[0].id, self.user, CertificateStatuses.error, mode)

        # Re-submit all certificates with status 'error'
        call_command(self.command)

        # Expect that the certificate was re-submitted
        self._assert_cert_status(self.courses[0].id, self.user, CertificateStatuses.notpassing)

    def test_resubmit_error_certificate_in_a_course(self):
        # Create a certificate with status 'error'
        # in three courses.
        for idx in range(3):
            self._create_cert(self.courses[idx].id, self.user, CertificateStatuses.error)

        # Re-submit certificates for two of the courses
        call_command(self.command, course_key_list=[
            str(self.courses[0].id),
            str(self.courses[1].id)
        ])

        # Expect that the first two courses have been re-submitted,
        # but not the third course.
        self._assert_cert_status(self.courses[0].id, self.user, CertificateStatuses.notpassing)
        self._assert_cert_status(self.courses[1].id, self.user, CertificateStatuses.notpassing)
        self._assert_cert_status(self.courses[2].id, self.user, CertificateStatuses.error)

    @ddt.data(
        CertificateStatuses.deleted,
        CertificateStatuses.deleting,
        CertificateStatuses.downloadable,
        CertificateStatuses.generating,
        CertificateStatuses.notpassing,
        CertificateStatuses.restricted,
        CertificateStatuses.unavailable,
    )
    def test_resubmit_error_certificate_skips_non_error_certificates(self, other_status):
        # Create certificates with an error status and some other status
        self._create_cert(self.courses[0].id, self.user, CertificateStatuses.error)
        self._create_cert(self.courses[1].id, self.user, other_status)

        # Re-submit certificates for all courses
        call_command(self.command)

        # Only the certificate with status "error" should have been re-submitted
        self._assert_cert_status(self.courses[0].id, self.user, CertificateStatuses.notpassing)
        self._assert_cert_status(self.courses[1].id, self.user, other_status)

    def test_resubmit_error_certificate_none_found(self):
        self._create_cert(self.courses[0].id, self.user, CertificateStatuses.downloadable)
        call_command(self.command)
        self._assert_cert_status(self.courses[0].id, self.user, CertificateStatuses.downloadable)

    def test_course_caching(self):
        # Create multiple certificates for the same course
        self._create_cert(self.courses[0].id, UserFactory.create(), CertificateStatuses.error)
        self._create_cert(self.courses[0].id, UserFactory.create(), CertificateStatuses.error)
        self._create_cert(self.courses[0].id, UserFactory.create(), CertificateStatuses.error)

        course_overview = CourseOverviewFactory.create(
            id=self.courses[0].id
        )

        with patch(
            'lms.djangoapps.certificates.management.commands.resubmit_error_certificates.get_course_overview_or_none'
        ) as mock_get_course_overview:
            mock_get_course_overview.return_value = course_overview

            call_command(self.command)

            mock_get_course_overview.assert_called_once()

    def test_invalid_course_key(self):
        invalid_key = "invalid/"
        with self.assertRaisesRegex(CommandError, invalid_key):
            call_command(self.command, course_key_list=[invalid_key])


@ddt.ddt
class RegenerateCertificatesTest(CertificateManagementTest):
    """
    Tests for regenerating certificates.
    """
    command = 'regenerate_user'

    def setUp(self):
        """
        We just need one course here.
        """
        super().setUp()
        self.course = self.courses[0]

    @ddt.data(True, False)
    @override_settings(CERT_QUEUE='test-queue')
    @patch.dict('django.conf.settings.FEATURES', {'ENABLE_OPENBADGES': True})
    @patch('lms.djangoapps.certificates.generation_handler.XQueueCertInterface', spec=True)
    def test_clear_badge(self, issue_badges, xqueue):
        """
        Given that I have a user with a badge
        If I run regeneration for a user
        Then certificate generation will be requested
        And the badge will be deleted if badge issuing is enabled
        """
        key = self.course.location.course_key
        self._create_cert(key, self.user, CertificateStatuses.downloadable)
        badge_class = get_completion_badge(key, self.user)
        BadgeAssertionFactory(badge_class=badge_class, user=self.user)
        assert BadgeAssertion.objects.filter(user=self.user, badge_class=badge_class)
        self.course.issue_badges = issue_badges
        self.store.update_item(self.course, None)

        args = f'-u {self.user.email} -c {str(key)}'
        call_command(self.command, *args.split(' '))

        assert xqueue.return_value.regen_cert.call_args.args == (
            self.user,
            key,
        )
        regen_cert_call_kwargs = xqueue.return_value.regen_cert.call_args.kwargs
        assert regen_cert_call_kwargs == {
            'forced_grade': None,
            'template_file': None,
            'generate_pdf': True,
        }

        assert bool(BadgeAssertion.objects.filter(user=self.user, badge_class=badge_class)) == (not issue_badges)

    @override_settings(CERT_QUEUE='test-queue')
    @patch('capa.xqueue_interface.XQueueInterface.send_to_queue', spec=True)
    def test_regenerating_certificate(self, mock_send_to_queue):
        """
        Given that I have a user who has not passed course
        If I run regeneration for that user
        Then certificate generation will be not be requested
        """
        key = self.course.location.course_key
        self._create_cert(key, self.user, CertificateStatuses.downloadable)

        args = f'-u {self.user.email} -c {str(key)} --insecure'
        call_command(self.command, *args.split(' '))

        certificate = GeneratedCertificate.eligible_certificates.get(
            user=self.user,
            course_id=key
        )
        assert certificate.status == CertificateStatuses.notpassing
        assert not mock_send_to_queue.called


class UngenerateCertificatesTest(CertificateManagementTest):
    """
    Tests for generating certificates.
    """
    command = 'ungenerated_certs'

    def setUp(self):
        """
        We just need one course here.
        """
        super().setUp()
        self.course = self.courses[0]

    @override_settings(CERT_QUEUE='test-queue')
    @patch('capa.xqueue_interface.XQueueInterface.send_to_queue', spec=True)
    def test_ungenerated_certificate(self, mock_send_to_queue):
        """
        Given that I have ended course
        If I run ungenerated certs command
        Then certificates should be generated for all users who passed course
        """
        mock_send_to_queue.return_value = (0, "Successfully queued")
        key = self.course.location.course_key
        self._create_cert(key, self.user, CertificateStatuses.unavailable)

        with mock_passing_grade():
            args = f'-c {str(key)} --insecure'
            call_command(self.command, *args.split(' '))

        assert mock_send_to_queue.called
        certificate = GeneratedCertificate.eligible_certificates.get(
            user=self.user,
            course_id=key
        )
        assert certificate.status == CertificateStatuses.generating
