"""
Unit tests for enabling self-generated certificates for self-paced courses
and disabling for instructor-paced courses.
"""


from unittest import mock

import ddt
from edx_toggles.toggles import LegacyWaffleSwitch
from edx_toggles.toggles.testutils import override_waffle_flag, override_waffle_switch

from common.djangoapps.student.tests.factories import CourseEnrollmentFactory, UserFactory
from lms.djangoapps.certificates.api import cert_generation_enabled
from lms.djangoapps.certificates.generation_handler import CERTIFICATES_USE_UPDATED
from lms.djangoapps.certificates.data import CertificateStatuses
from lms.djangoapps.certificates.models import (
    CertificateGenerationConfiguration,
    GeneratedCertificate
)
from lms.djangoapps.certificates.signals import _fire_ungenerated_certificate_task
from lms.djangoapps.certificates.tasks import CERTIFICATE_DELAY_SECONDS
from lms.djangoapps.certificates.tests.factories import CertificateAllowlistFactory, GeneratedCertificateFactory
from lms.djangoapps.grades.course_grade_factory import CourseGradeFactory
from lms.djangoapps.grades.tests.utils import mock_passing_grade
from lms.djangoapps.verify_student.models import SoftwareSecurePhotoVerification
from openedx.core.djangoapps.certificates.config import waffle
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

AUTO_CERTIFICATE_GENERATION_SWITCH = LegacyWaffleSwitch(waffle.waffle(), waffle.AUTO_CERTIFICATE_GENERATION)  # lint-amnesty, pylint: disable=toggle-missing-annotation


class SelfGeneratedCertsSignalTest(ModuleStoreTestCase):
    """
    Tests for enabling/disabling self-generated certificates according to course-pacing.
    """
    ENABLED_SIGNALS = ['course_published']

    def setUp(self):
        super().setUp()
        CertificateGenerationConfiguration.objects.create(enabled=True)

    def test_cert_generation_flag_on_pacing_toggle(self):
        """
        Verify that signal enables or disables self-generated certificates
        according to course-pacing.
        """
        course = CourseFactory.create(self_paced=False, emit_signals=True)
        assert not cert_generation_enabled(course.id)

        course.self_paced = True
        self.store.update_item(course, self.user.id)
        assert cert_generation_enabled(course.id)

        course.self_paced = False
        self.store.update_item(course, self.user.id)
        assert not cert_generation_enabled(course.id)


class AllowlistGeneratedCertificatesTest(ModuleStoreTestCase):
    """
    Tests for allowlisted student auto-certificate generation
    """

    def setUp(self):
        super().setUp()
        self.user = UserFactory.create()
        # Instructor paced course
        self.ip_course = CourseFactory.create(self_paced=False)
        CourseEnrollmentFactory(
            user=self.user,
            course_id=self.ip_course.id,
            is_active=True,
            mode="verified",
        )

    def test_fire_task_allowlist_auto_enabled(self):
        """
        Test that the allowlist generation is invoked if automatic generation is enabled
        """
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
            return_value=None
        ) as mock_generate_certificate_apply_async:
            with mock.patch(
                'lms.djangoapps.certificates.signals.generate_allowlist_certificate_task',
                return_value=None
            ) as mock_generate_allowlist_task:
                with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                    CertificateAllowlistFactory(
                        user=self.user,
                        course_id=self.ip_course.id
                    )
                    mock_generate_certificate_apply_async.assert_not_called()
                    mock_generate_allowlist_task.assert_called_with(self.user, self.ip_course.id)

    def test_fire_task_allowlist_auto_disabled(self):
        """
        Test that the allowlist generation is not invoked if automatic generation is disabled
        """
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
            return_value=None
        ) as mock_generate_certificate_apply_async:
            with mock.patch(
                'lms.djangoapps.certificates.signals.generate_allowlist_certificate_task',
                return_value=None
            ) as mock_generate_allowlist_task:
                with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=False):
                    CertificateAllowlistFactory(
                        user=self.user,
                        course_id=self.ip_course.id
                    )
                    mock_generate_certificate_apply_async.assert_not_called()
                    mock_generate_allowlist_task.assert_not_called()


class PassingGradeCertsTest(ModuleStoreTestCase):
    """
    Tests for certificate generation task firing on passing grade receipt
    """

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(
            self_paced=True,
        )
        self.course_key = self.course.id
        self.user = UserFactory.create()
        self.enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course.id,
            is_active=True,
            mode="verified",
        )
        self.ip_course = CourseFactory.create(self_paced=False)
        self.ip_enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.ip_course.id,
            is_active=True,
            mode="verified",
        )
        attempt = SoftwareSecurePhotoVerification.objects.create(
            user=self.user,
            status='submitted'
        )
        attempt.approve()

    def test_cert_generation_on_passing_v1(self):
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
            return_value=None
        ) as mock_generate_certificate_apply_async:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                grade_factory = CourseGradeFactory()
                # Not passing
                grade_factory.update(self.user, self.ip_course)
                mock_generate_certificate_apply_async.assert_not_called()
                # Certs fired after passing
                with mock_passing_grade():
                    grade_factory.update(self.user, self.ip_course)
                    mock_generate_certificate_apply_async.assert_called_with(
                        countdown=CERTIFICATE_DELAY_SECONDS,
                        kwargs={
                            'student': str(self.user.id),
                            'course_key': str(self.ip_course.id),
                        }
                    )

    def test_cert_already_generated(self):
        with mock.patch(
                'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
                return_value=None
        ) as mock_generate_certificate_apply_async:
            grade_factory = CourseGradeFactory()
            # Create the certificate
            GeneratedCertificateFactory(
                user=self.user,
                course_id=self.course.id,
                status=CertificateStatuses.downloadable
            )
            # Certs are not re-fired after passing
            with mock_passing_grade():
                grade_factory.update(self.user, self.course)
                mock_generate_certificate_apply_async.assert_not_called()

    def test_passing_grade_allowlist(self):
        with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
            # User who is not on the allowlist
            GeneratedCertificateFactory(
                user=self.user,
                course_id=self.course.id,
                status=CertificateStatuses.error
            )
            with mock_passing_grade():
                with mock.patch(
                    'lms.djangoapps.certificates.signals.generate_certificate_task',
                    return_value=None
                ) as mock_cert_task:
                    CourseGradeFactory().update(self.user, self.course)
                    mock_cert_task.assert_not_called()

            # User who is on the allowlist
            u = UserFactory.create()
            c = CourseFactory()
            course_key = c.id  # pylint: disable=no-member
            CertificateAllowlistFactory(
                user=u,
                course_id=course_key
            )
            GeneratedCertificateFactory(
                user=u,
                course_id=course_key,
                status=CertificateStatuses.error
            )
            with mock_passing_grade():
                with mock.patch(
                    'lms.djangoapps.certificates.signals.generate_certificate_task',
                    return_value=None
                ) as mock_cert_task:
                    CourseGradeFactory().update(u, c)
                    mock_cert_task.assert_called_with(u, course_key)

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=True)
    def test_cert_already_generated_downloadable(self):
        with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
            GeneratedCertificateFactory(
                user=self.user,
                course_id=self.course.id,
                status=CertificateStatuses.downloadable
            )

            with mock.patch(
                'lms.djangoapps.certificates.signals.generate_certificate_task',
                return_value=None
            ) as mock_cert_task:
                grade_factory = CourseGradeFactory()
                with mock_passing_grade():
                    grade_factory.update(self.user, self.course)
                    mock_cert_task.assert_not_called()

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=True)
    def test_cert_already_generated_unverified(self):
        with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
            GeneratedCertificateFactory(
                user=self.user,
                course_id=self.course.id,
                status=CertificateStatuses.unverified
            )

            with mock.patch(
                'lms.djangoapps.certificates.signals.generate_certificate_task',
                return_value=None
            ) as mock_cert_task:
                grade_factory = CourseGradeFactory()
                with mock_passing_grade():
                    grade_factory.update(self.user, self.course)
                    mock_cert_task.assert_called_with(self.user, self.course_key)

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=True)
    def test_without_cert(self):
        with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
            with mock.patch(
                'lms.djangoapps.certificates.signals.generate_certificate_task',
                return_value=None
            ) as mock_cert_task:
                grade_factory = CourseGradeFactory()
                with mock_passing_grade():
                    grade_factory.update(self.user, self.course)
                    mock_cert_task.assert_called_with(self.user, self.course_key)


@ddt.ddt
class FailingGradeCertsTest(ModuleStoreTestCase):
    """
    Tests for marking certificate notpassing when grade goes from passing to failing,
    and that the signal has no effect on the cert status if the cert has a non-passing
    status
    """

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(
            self_paced=True,
        )
        self.user = UserFactory.create()
        self.enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course.id,
            is_active=True,
            mode="verified",
        )
        attempt = SoftwareSecurePhotoVerification.objects.create(
            user=self.user,
            status='submitted'
        )
        attempt.approve()

    @ddt.data(
        CertificateStatuses.deleted,
        CertificateStatuses.deleting,
        CertificateStatuses.downloadable,
        CertificateStatuses.error,
        CertificateStatuses.generating,
        CertificateStatuses.notpassing,
        CertificateStatuses.restricted,
        CertificateStatuses.unavailable,
        CertificateStatuses.auditing,
        CertificateStatuses.audit_passing,
        CertificateStatuses.audit_notpassing,
        CertificateStatuses.unverified,
        CertificateStatuses.invalidated,
        CertificateStatuses.requesting,
    )
    def test_cert_failure(self, status):
        if CertificateStatuses.is_passing_status(status):
            expected_status = CertificateStatuses.notpassing
        else:
            expected_status = status
        GeneratedCertificateFactory(
            user=self.user,
            course_id=self.course.id,
            status=status
        )
        CourseGradeFactory().update(self.user, self.course)
        cert = GeneratedCertificate.certificate_for_student(self.user, self.course.id)
        assert cert.status == expected_status

    def test_failing_grade_allowlist(self):
        # User who is not on the allowlist
        GeneratedCertificateFactory(
            user=self.user,
            course_id=self.course.id,
            status=CertificateStatuses.downloadable
        )
        CourseGradeFactory().update(self.user, self.course)
        cert = GeneratedCertificate.certificate_for_student(self.user, self.course.id)
        assert cert.status == CertificateStatuses.notpassing

        # User who is on the allowlist
        u = UserFactory.create()
        c = CourseFactory()
        course_key = c.id  # pylint: disable=no-member
        CertificateAllowlistFactory(
            user=u,
            course_id=course_key
        )
        GeneratedCertificateFactory(
            user=u,
            course_id=course_key,
            status=CertificateStatuses.downloadable
        )
        CourseGradeFactory().update(u, c)
        cert = GeneratedCertificate.certificate_for_student(u, course_key)
        assert cert.status == CertificateStatuses.downloadable


class LearnerIdVerificationTest(ModuleStoreTestCase):
    """
    Tests for certificate generation task firing on learner id verification
    """

    def setUp(self):
        super().setUp()
        self.course_one = CourseFactory.create(self_paced=True)
        self.user_one = UserFactory.create()
        self.enrollment_one = CourseEnrollmentFactory(
            user=self.user_one,
            course_id=self.course_one.id,
            is_active=True,
            mode='verified',
        )
        self.user_two = UserFactory.create()
        self.course_two = CourseFactory.create(self_paced=False)
        self.enrollment_two = CourseEnrollmentFactory(
            user=self.user_two,
            course_id=self.course_two.id,
            is_active=True,
            mode='verified'
        )
        with mock_passing_grade():
            grade_factory = CourseGradeFactory()
            grade_factory.update(self.user_one, self.course_one)
            grade_factory.update(self.user_two, self.course_two)

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=True)
    def test_cert_generation_on_photo_verification(self):
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate_task',
            return_value=None
        ) as mock_cert_task:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                attempt = SoftwareSecurePhotoVerification.objects.create(
                    user=self.user_two,
                    status='submitted'
                )
                attempt.approve()
                mock_cert_task.assert_called_with(self.user_two, self.course_two.id)

    def test_cert_generation_on_photo_verification_v1(self):
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
            return_value=None
        ) as mock_cert_task:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                attempt = SoftwareSecurePhotoVerification.objects.create(
                    user=self.user_two,
                    status='submitted'
                )
                attempt.approve()
                mock_cert_task.assert_called_with(
                    countdown=CERTIFICATE_DELAY_SECONDS,
                    kwargs={
                        'student': str(self.user_two.id),
                        'course_key': str(self.course_two.id),
                        'expected_verification_status': 'approved'
                    }
                )

    def test_id_verification_allowlist(self):
        # User is not on the allowlist
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_allowlist_certificate_task',
            return_value=None
        ) as mock_allowlist_task:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                attempt = SoftwareSecurePhotoVerification.objects.create(
                    user=self.user_two,
                    status='submitted'
                )
                attempt.approve()
                mock_allowlist_task.assert_not_called()

        # User is on the allowlist
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_allowlist_certificate_task',
            return_value=None
        ) as mock_allowlist_task:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                u = UserFactory.create()
                c = CourseFactory()
                course_key = c.id  # pylint: disable=no-member
                CourseEnrollmentFactory(
                    user=u,
                    course_id=course_key,
                    is_active=True,
                    mode='verified'
                )
                CertificateAllowlistFactory(
                    user=u,
                    course_id=course_key
                )
                attempt = SoftwareSecurePhotoVerification.objects.create(
                    user=u,
                    status='submitted'
                )
                attempt.approve()
                mock_allowlist_task.assert_called_with(u, course_key)


@ddt.ddt
class CertificateGenerationTaskTest(ModuleStoreTestCase):
    """
    Tests for certificate generation task.
    """

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create()

    @ddt.data(
        ('professional', True),
        ('verified', True),
        ('no-id-professional', True),
        ('credit', True),
        ('masters', True),
        ('audit', False),
        ('honor', False),
    )
    @ddt.unpack
    def test_fire_ungenerated_certificate_task_allowed_modes(self, enrollment_mode, should_create):
        """
        Test that certificate generation task is fired for only modes that are
        allowed to generate certificates automatically.
        """
        self.user = UserFactory.create()
        CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course.id,
            is_active=True,
            mode=enrollment_mode
        )
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate.apply_async',
            return_value=None
        ) as mock_generate_certificate_apply_async:
            with override_waffle_switch(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True):
                _fire_ungenerated_certificate_task(self.user, self.course.id)
                task_created = mock_generate_certificate_apply_async.called
                assert task_created == should_create


@override_waffle_flag(AUTO_CERTIFICATE_GENERATION_SWITCH, active=True)
class EnrollmentModeChangeCertsTest(ModuleStoreTestCase):
    """
    Tests for certificate generation task firing when the user's enrollment mode changes
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory.create()
        self.verified_course = CourseFactory.create(
            self_paced=True,
        )
        self.verified_course_key = self.verified_course.id  # pylint: disable=no-member
        self.verified_enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.verified_course_key,
            is_active=True,
            mode='verified',
        )
        CertificateAllowlistFactory(
            user=self.user,
            course_id=self.verified_course_key
        )

        self.audit_course = CourseFactory.create(self_paced=False)
        self.audit_course_key = self.audit_course.id  # pylint: disable=no-member
        self.audit_enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.audit_course_key,
            is_active=True,
            mode='audit',
        )
        CertificateAllowlistFactory(
            user=self.user,
            course_id=self.audit_course_key
        )

    def test_audit_to_verified(self):
        """
        Test that we try to generate a certificate when the user switches from audit to verified
        """
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_certificate_task',
            return_value=None
        ) as mock_cert_task:
            self.audit_enrollment.change_mode('verified')
            mock_cert_task.assert_called_with(self.user, self.audit_course_key)

    def test_verified_to_audit(self):
        """
        Test that we do not try to generate a certificate when the user switches from verified to audit
        """
        with mock.patch(
            'lms.djangoapps.certificates.signals.generate_allowlist_certificate_task',
            return_value=None
        ) as mock_allowlist_task:
            self.verified_enrollment.change_mode('audit')
            mock_allowlist_task.assert_not_called()
