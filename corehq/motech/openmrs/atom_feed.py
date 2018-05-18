from __future__ import absolute_import
from __future__ import unicode_literals

import re
import uuid
from collections import namedtuple
from datetime import datetime

from dateutil import parser as dateutil_parser
from dateutil.tz import tzutc
from lxml import etree
from requests import RequestException

from casexml.apps.case.mock import CaseBlock
from corehq.apps.case_importer import util as importer_util
from corehq.apps.case_importer.const import LookupErrors
from corehq.apps.case_importer.util import EXTERNAL_ID
from corehq.apps.hqcase.utils import submit_case_blocks
from corehq.apps.locations.dbaccessors import get_one_commcare_user_at_location
from corehq.motech.openmrs.const import XMLNS_OPENMRS, OPENMRS_ATOM_FEED_DEVICE_ID
from corehq.motech.openmrs.openmrs_config import get_property_map
from corehq.motech.openmrs.repeater_helpers import Requests, get_patient_by_uuid
from corehq.motech.openmrs.repeaters import OpenmrsRepeater
from corehq.util.soft_assert import soft_assert


UuidUpdatedAt = namedtuple('UuidUpdatedAt', 'uuid updated_at')
_assert = soft_assert(['@'.join(('nhooper', 'dimagi.com'))])


def get_patient_feed_xml(requests, page):
    if not page:
        # If this is the first time the patient feed is polled, just get
        # the most recent changes. This shows updating patients
        # successfully, but does not replay all OpenMRS changes.
        page = 'recent'
    patient_feed_url = '/ws/atomfeed/patient/' + page
    resp = requests.get(patient_feed_url)
    root = etree.fromstring(resp.content)
    return root


def get_timestamp(element, xpath='./updated'):
    """
    Returns a datetime instance of the text at the given xpath.

    >>> element = etree.XML('<feed><updated>2018-05-15T14:02:08Z</updated></feed>')
    >>> get_timestamp(element)
    datetime.datetime(2018, 5, 15, 14, 2, 8, tzinfo=tzutc())

    """
    timestamp_elems = element.xpath(xpath)
    if timestamp_elems and len(timestamp_elems) == 1:
        tzinfos = {'UTC': tzutc()}
        return dateutil_parser.parse(timestamp_elems[0].text, tzinfos=tzinfos)


def get_patient_uuid(element):
    """
    Extracts the UUID of a patient from an entry's "content" node.

    >>> element = etree.XML('''<entry>
    ...     <content type="application/vnd.atomfeed+xml">
    ...         <![CDATA[/openmrs/ws/rest/v1/patient/e8aa08f6-86cd-42f9-8924-1b3ea021aeb4?v=full]]>
    ...     </content>
    ... </entry>''')
    >>> get_patient_uuid(element) == 'e8aa08f6-86cd-42f9-8924-1b3ea021aeb4'
    True

    """
    content = element.xpath('./content')
    pattern = re.compile(r'/patient/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b')
    if content and len(content) == 1:
        cdata = content[0].text
        matches = pattern.search(cdata)
        if matches:
            return matches.group(1)


def get_updated_patients(repeater):
    """
    Iterates over a paginated atom feed, yields patients updated since
    repeater.atom_feed_last_polled_at, and updates the repeater.
    """
    requests = Requests(repeater.url, repeater.username, repeater.password)
    last_polled_at = repeater.atom_feed_last_polled_at
    page = repeater.atom_feed_last_page
    try:
        while True:
            feed_xml = get_patient_feed_xml(requests, page)
            if (
                not last_polled_at or
                get_timestamp(feed_xml) > last_polled_at
            ):
                for entry in feed_xml.xpath('./entry'):
                    if (
                        not last_polled_at or
                        get_timestamp(entry, './published') > last_polled_at
                    ):
                        yield UuidUpdatedAt(
                            get_patient_uuid(entry),
                            get_timestamp(entry, './updated')
                        )
            next_page = feed_xml.xpath('./link[rel="next-archive"]')
            if next_page:
                href = next_page[0]['href']
                page = href.split('/')[-1]
            else:
                if not page:
                    this_page = feed_xml.xpath('./link[rel="via"]')
                    href = this_page[0]['href']
                    page = href.split('/')[-1]
                break
    except RequestException:
        # Don't update repeater if OpenMRS is offline
        return
    else:
        repeater.atom_feed_last_polled_at = datetime.utcnow()
        repeater.atom_feed_last_page = page
        repeater.save()


def get_addpatient_caseblock(case_type, owner, patient, repeater):
    property_map = get_property_map(repeater.openmrs_config.case_config)

    fields_to_update = {}
    for prop, (jsonpath, value_map) in property_map.items():
        matches = jsonpath.find(patient)
        for match in matches:
            patient_value = match.value
            new_value = value_map.get(patient_value, patient_value)
            fields_to_update[prop] = new_value

    if fields_to_update:
        case_id = uuid.uuid4().hex
        case_name = patient['person']['display']
        return CaseBlock(
            create=True,
            case_id=case_id,
            owner_id=owner.user_id,
            user_id=owner.user_id,
            case_type=case_type,
            case_name=case_name,
            external_id=patient['uuid'],
            update=fields_to_update,
        )


def get_updatepatient_caseblock(case, patient, repeater):
    property_map = get_property_map(repeater.openmrs_config.case_config)

    fields_to_update = {}
    for prop, (jsonpath, value_map) in property_map.items():
        matches = jsonpath.find(patient)
        for match in matches:
            patient_value = match.value
            case_value = case.get_case_property(prop)
            new_value = value_map.get(patient_value, patient_value)
            if case_value != new_value:
                fields_to_update[prop] = new_value

    if fields_to_update:
        case_name = patient['person']['display']
        return CaseBlock(
            create=False,
            case_id=case.get_id,
            case_name=case_name,
            update=fields_to_update,
        )


def update_patient(repeater, patient_uuid, updated_at):
    """
    Fetch patient from OpenMRS, submit case update for all mapped case
    properties.

    .. NOTE:: OpenMRS UUID must be saved to "external_id" case property

    """
    _assert(
        len(repeater.white_listed_case_types) == 1,
        'Unable to update patients from OpenMRS unless a single case type is '
        'specified. domain: "{}". repeater: "{}".'.format(
            repeater.domain, repeater.get_id
        )
    )
    case_type = repeater.white_listed_case_types[0]
    requests = Requests(repeater.url, repeater.username, repeater.password)
    patient = get_patient_by_uuid(requests, patient_uuid)
    case, error = importer_util.lookup_case(
        EXTERNAL_ID,
        patient_uuid,
        repeater.domain,
        case_type=case_type,
    )
    if error == LookupErrors.NotFound:
        owner = get_one_commcare_user_at_location(repeater.domain, repeater.location_id)
        _assert(
            owner,
            'No users found at location "{}" to own patients added from '
            'OpenMRS atom feed. domain: "{}". repeater: "{}".'.format(
                repeater.location_id, repeater.domain, repeater.get_id
            )
        )
        case_block = get_addpatient_caseblock(case_type, owner, patient, repeater)

    else:
        _assert(
            error != LookupErrors.MultipleResults,
            # Multiple cases matched to the same patient.
            # Could be caused by:
            # * The cases were given the same identifier value. It could
            #   be user error, or case config assumed identifier was
            #   unique but it wasn't.
            # * PatientFinder matched badly.
            # * Race condition where a patient was previously added to
            #   both CommCare and OpenMRS.
            'More than one case found matching unique OpenMRS UUID. '
            'domain: "{}". case external_id: "{}". repeater: "{}".'.format(
                repeater.domain, patient_uuid, repeater.get_id
            )
        )
        case_block = get_updatepatient_caseblock(case, patient, repeater)

    if case_block:
        submit_case_blocks(
            [case_block.as_string()],
            repeater.domain,
            xmlns=XMLNS_OPENMRS,
            device_id=OPENMRS_ATOM_FEED_DEVICE_ID + repeater.get_id,
        )


def poll_openmrs_atom_feeds(domain_name):
    for repeater in OpenmrsRepeater.by_domain(domain_name):
        if repeater.atom_feed_enabled:
            updated_patients = get_updated_patients(repeater)
            for patient_uuid, updated_at in updated_patients:
                update_patient(repeater, patient_uuid, updated_at)
