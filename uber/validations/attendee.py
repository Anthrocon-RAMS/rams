import re

from datetime import date
from pockets import classproperty
from wtforms import validators
from wtforms.validators import ValidationError, StopValidation

from uber.badge_funcs import get_real_badge_type
from uber.config import c
from uber.custom_tags import format_currency
from uber.models import Attendee, Session, PromoCode, PromoCodeGroup
from uber.model_checks import invalid_zip_code, invalid_phone_number
from uber.utils import get_age_from_birthday, get_age_conf_from_birthday
from uber.decorators import WTFormValidation

form_validation, new_or_changed_validation, post_form_validation = WTFormValidation(), WTFormValidation(), WTFormValidation()

"""
These should probably be rewritten as automatic changes with a message attached
@post_form_validation.badge_type
def child_badge_over_13(attendee):
    if c.CHILD_BADGE in c.PREREG_BADGE_TYPES and attendee.badge_type == c.CHILD_BADGE \
            and get_age_from_birthday(attendee.birthdate, c.NOW_OR_AT_CON) >= 13:
        raise ValidationError("If you will be 13 or older at the start of {}, " \
                                "please select an Attendee badge instead of a 12 and Under badge.".format(c.EVENT_NAME))

@post_form_validation.badge_type
def attendee_badge_under_13(attendee):
    if c.CHILD_BADGE in c.PREREG_BADGE_TYPES and attendee.badge_type == c.ATTENDEE_BADGE \
        and get_age_from_birthday(attendee.birthdate, c.NOW_OR_AT_CON) < 13:
        raise ValidationError("If you will be 12 or younger at the start of {}, " \
                                "please select the 12 and Under badge instead of an Attendee badge.".format(c.EVENT_NAME))
"""

###### Attendee-Facing Validations ######
def attendee_age_checks(form, field):
    age_group_conf = get_age_conf_from_birthday(field.data, c.NOW_OR_AT_CON) \
        if (hasattr(form, "birthdate") and form.birthdate.data) else field.data
    if age_group_conf and not age_group_conf['can_register']:
        raise ValidationError('Attendees {} years of age do not need to register, ' \
            'but MUST be accompanied by a parent at all times!'.format(age_group_conf['desc'].lower()))

@post_form_validation.none
def reasonable_total_cost(attendee):
    if attendee.total_cost >= 999999:
        raise ValidationError('We cannot charge {}. Please reduce extras so the total is below $9,999.'.format(
            format_currency(attendee.total_cost)))

@post_form_validation.none
def allowed_to_register(attendee):
    if not attendee.age_group_conf['can_register']:
        raise ValidationError('Attendees {} years of age do not need to register, ' \
            'but MUST be accompanied by a parent at all times!'.format(attendee.age_group_conf['desc'].lower()))

@new_or_changed_validation.amount_extra
def upgrade_sold_out(form, field):
    currently_available_upgrades = [tier['value'] for tier in c.PREREG_DONATION_DESCRIPTIONS]
    if field.data and field.data not in currently_available_upgrades:
        raise ValidationError("The upgrade you have selected is sold out.")

@post_form_validation.badge_type
def child_group_leaders(attendee):
    if attendee.badge_type == c.PSEUDO_GROUP_BADGE and get_age_from_birthday(attendee.birthdate, c.NOW_OR_AT_CON) < 13:
        raise ValidationError("Children under 13 cannot be group leaders.")
"""
@post_form_validation.badge_type
def no_more_child_badges(attendee):
    # TODO: Review business logic here
    if c.CHILD_BADGE in c.PREREG_BADGE_TYPES and get_age_from_birthday(attendee.birthdate, c.NOW_OR_AT_CON) < 18 \
            and not c.CHILD_BADGE_AVAILABLE:
        raise ValidationError("Unfortunately, we are sold out of badges for attendees under 18.")
"""

@new_or_changed_validation.badge_type
def no_more_custom_badges(form, field):
    if field.data in c.PREASSIGNED_BADGE_TYPES and c.AFTER_PRINTED_BADGE_DEADLINE:
        with Session() as session:
            admin = session.current_admin_account()
            if admin.is_super_admin:
                return
        raise ValidationError('Custom badges have already been ordered, please choose a different badge type.')

@new_or_changed_validation.badge_type
def out_of_badge_type(form, field):
    badge_type = get_real_badge_type(field.data)
    with Session() as session:
        try:
            session.get_next_badge_num(badge_type)
        except AssertionError:
            raise ValidationError('We are sold out of {} badges.'.format(c.BADGES[badge_type]))

@new_or_changed_validation.badge_printed_name
def past_printed_deadline(form, field):
    if field.data in c.PREASSIGNED_BADGE_TYPES and c.PRINTED_BADGE_DEADLINE and c.AFTER_PRINTED_BADGE_DEADLINE:
        with Session() as session:
            admin = session.current_admin_account()
            if admin.is_super_admin:
                return
        raise ValidationError('{} badges have already been ordered, so you cannot change your printed badge name.'.format(
            c.BADGES[field.data]))

@post_form_validation.birthdate
def age_discount_after_paid(attendee):
    if (attendee.total_cost * 100) < attendee.amount_paid:
        if (not attendee.orig_value_of('birthdate') or attendee.orig_value_of('birthdate') < attendee.birthdate) \
                and attendee.age_group_conf['discount'] > 0:
            raise ValidationError('The date of birth you entered incurs a discount; \
                                  please email {} to change your badge and receive a refund'.format(c.REGDESK_EMAIL))

@post_form_validation.cellphone
def volunteers_cellphone_or_checkbox(attendee):
    if not attendee.no_cellphone and attendee.staffing_or_will_be and not attendee.cellphone:
        raise ValidationError("Volunteers and staffers must provide a cellphone number or indicate they do not have a cellphone.")


@post_form_validation.promo_code
def promo_code_is_useful(attendee):
    if attendee.promo_code:
        with Session() as session:
            if session.lookup_agent_code(attendee.promo_code.code):
                return
            code = session.lookup_promo_or_group_code(attendee.promo_code.code, PromoCode)
            group = code.group if code and code.group else session.lookup_promo_or_group_code(attendee.promo_code.code, PromoCodeGroup)
            if group and group.total_cost == 0:
                return

    if attendee.is_new and attendee.promo_code:
        if not attendee.is_unpaid:
            raise ValidationError("You can't apply a promo code after you've paid or if you're in a group.")
        elif attendee.is_dealer:
            raise ValidationError("You can't apply a promo code to a {}.".format(c.DEALER_REG_TERM))
        elif attendee.age_discount != 0:
            raise ValidationError("You are already receiving an age based discount, you can't use a promo code on top of that.")
        elif attendee.badge_type == c.ONE_DAY_BADGE or attendee.is_presold_oneday:
            raise ValidationError("You can't apply a promo code to a one day badge.")
        elif attendee.overridden_price:
            raise ValidationError("You already have a special badge price, you can't use a promo code on top of that.")
        elif attendee.default_badge_cost >= attendee.badge_cost_without_promo_code:
            raise ValidationError("That promo code doesn't make your badge any cheaper. You may already have other discounts.")


@post_form_validation.promo_code
def promo_code_not_is_expired(attendee):
    if attendee.is_new and attendee.promo_code and attendee.promo_code.is_expired:
        raise ValidationError('That promo code is expired.')


@post_form_validation.promo_code
def promo_code_has_uses_remaining(attendee):
    from uber.payments import PreregCart
    if attendee.is_new and attendee.promo_code and not attendee.promo_code.is_unlimited:
        unpaid_uses_count = PreregCart.get_unpaid_promo_code_uses_count(
            attendee.promo_code.id, attendee.id)
        if (attendee.promo_code.uses_remaining - unpaid_uses_count) < 0:
            raise ValidationError('That promo code has been used too many times.')


@post_form_validation.staffing
def allowed_to_volunteer(attendee):
    if attendee.staffing_or_will_be \
            and not attendee.age_group_conf['can_volunteer'] \
            and attendee.badge_type not in [c.STAFF_BADGE, c.CONTRACTOR_BADGE] \
            and c.PRE_CON:
        raise ValidationError('Your interest is appreciated, but ' + c.EVENT_NAME + ' volunteers must be 18 or older.')


@post_form_validation.staffing
def banned_volunteer(attendee):
    if attendee.staffing_or_will_be and attendee.full_name in c.BANNED_STAFFERS:
        raise ValidationError("We've declined to invite {} back as a volunteer, ".format(attendee.full_name) + (
                    'talk to STOPS to override if necessary' if c.AT_THE_CON else
                    'Please contact us via {} if you believe this is in error'.format(c.CONTACT_URL)))


###### Admin-Only Validations ######
@post_form_validation.badge_num
def not_in_range(attendee):
    if not attendee.badge_num:
        return
    
    badge_type = get_real_badge_type(attendee.badge_type)
    lower_bound, upper_bound = c.BADGE_RANGES[badge_type]
    if not (lower_bound <= attendee.badge_num <= upper_bound):
        raise ValidationError('Badge number {} is out of range for badge type {} ({} - {})'.format(attendee.badge_num, 
                                                                                    c.BADGES[attendee.badge_type],
                                                                                    lower_bound, 
                                                                                    upper_bound))

@form_validation.badge_num
def dupe_badge_num(form, field):
    existing_name = ''
    if c.NUMBERED_BADGES and field.data \
            and (not c.SHIFT_CUSTOM_BADGES or c.AFTER_PRINTED_BADGE_DEADLINE or c.AT_THE_CON):
        with Session() as session:
            existing = session.query(Attendee).filter_by(badge_num=field.data)
            if not existing.count():
                return
            else:
                existing_name = existing.first().full_name
        raise ValidationError('That badge number already belongs to {!r}'.format(existing_name))

@post_form_validation.group_id
def dealer_needs_group(attendee):
    if attendee.is_dealer and not attendee.badge_type == c.PSEUDO_DEALER_BADGE and not attendee.group_id:
        raise ValidationError('{}s must be associated with a group'.format(c.DEALER_TERM))

@post_form_validation.group_id
def group_leadership(attendee):
    if attendee.session and not attendee.group_id:
        orig_group_id = attendee.orig_value_of('group_id')
        if orig_group_id and attendee.id == attendee.session.group(orig_group_id).leader_id:
            raise ValidationError('You cannot remove the leader of a group from that group; make someone else the leader first')