"""
Badges related signal handlers.
"""


from django.dispatch import receiver

from common.djangoapps.student.models import EnrollStatusChange
from common.djangoapps.student.signals import ENROLL_STATUS_CHANGE
from lms.djangoapps.badges.events.course_meta import award_enrollment_badge
from lms.djangoapps.badges.utils import badges_enabled
import logging

log = logging.getLogger(__name__)


@receiver(ENROLL_STATUS_CHANGE)
def award_badge_on_enrollment(sender, event=None, user=None, **kwargs):  # pylint: disable=unused-argument
    enabled = badges_enabled
    log.info('===========')
    log.info('ENROLL BADGE')
    log.info('badge enable: %s', enabled)
    log.info('event: %s', event)
    log.info('===========')
    """
    Awards enrollment badge to the given user on new enrollments.
    """
    if badges_enabled and event == EnrollStatusChange.enroll:
        log.info('===========')
        log.info('ENROLL BADGE 2')
        log.info('===========')
        award_enrollment_badge(user)
        log.info('===========')
        log.info('ENROLL BADGE COMPLETE')
        log.info('===========')
