"""
PatientFinders are used to find OpenMRS patients that correspond to
CommCare cases if none of the patient identifiers listed in
OpenmrsCaseConfig.match_on_ids have successfully matched a patient.

See `README.md`__ for more context.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from collections import namedtuple
from pprint import pformat

import six

from corehq.motech.openmrs.openmrs_config import get_property_map
from dimagi.ext.couchdbkit import (
    DecimalProperty,
    DocumentSchema,
    ListProperty,
    StringProperty,
)


class PatientFinder(DocumentSchema):
    """
    Subclasses of the PatientFinder class implement particular
    strategies for finding OpenMRS patients that suit a particular
    project. (WeightedPropertyPatientFinder was first subclass to be
    written. A future project with stronger emphasis on patient names
    might use Levenshtein distance, for example.)

    Subclasses must implement the `find_patients()` method.
    """

    @classmethod
    def wrap(cls, data):
        from corehq.motech.openmrs.openmrs_config import recurse_subclasses

        if cls is PatientFinder:
            return {
                sub._doc_type: sub for sub in recurse_subclasses(cls)
            }[data['doc_type']].wrap(data)
        else:
            return super(PatientFinder, cls).wrap(data)

    def find_patients(self, requests, case, case_config):
        """
        Given a case, search OpenMRS for possible matches. Return the
        best results. Subclasses must define "best". If just one result
        is returned, it will be chosen.

        NOTE:: False positives can result in overwriting one patient
               with the data of another. It is definitely better to
               return no results or multiple results than to return a
               single invalid result. Returned results should be
               logged.
        """
        raise NotImplementedError


PatientScore = namedtuple('PatientScore', ['patient', 'score'])


class PropertyWeight(DocumentSchema):
    case_property = StringProperty()
    weight = DecimalProperty()


class WeightedPropertyPatientFinder(PatientFinder):
    """
    Finds patients that match cases by assigning weights to matching
    property values, and adding those weights to calculate a confidence
    score.
    """

    # Identifiers that are searchable in OpenMRS. e.g.
    #     [ 'bahmni_id', 'household_id', 'last_name']
    searchable_properties = ListProperty()

    # The weight assigned to a matching property.
    # [
    #     {"case_property": "bahmni_id", "weight": 0.9},
    #     {"case_property": "household_id", "weight": 0.9},
    #     {"case_property": "dob", "weight": 0.75},
    #     {"case_property": "first_name", "weight": 0.025},
    #     {"case_property": "last_name", "weight": 0.025},
    #     {"case_property": "municipality", "weight": 0.2},
    # ]
    property_weights = ListProperty(PropertyWeight)

    # The threshold that the sum of weights must pass for a CommCare case to
    # be considered a match to an OpenMRS patient
    threshold = DecimalProperty(default=1.0)

    # If more than one patient passes `threshold`, the margin by which the
    # weight of the best match must exceed the weight of the second-best match
    # to be considered correct.
    confidence_margin = DecimalProperty(default=0.667)  # Default: Matches two thirds better than second-best

    def __init__(self, *args, **kwargs):
        super(WeightedPropertyPatientFinder, self).__init__(*args, **kwargs)
        self._property_map = {}

    def get_score(self, patient, case):
        """
        Return the sum of weighted properties to give an OpenMRS
        patient a score of how well they match a CommCare case.
        """
        def weights():
            for property_weight in self.property_weights:
                prop = property_weight['case_property']
                weight = property_weight['weight']

                matches = self._property_map[prop].jsonpath.find(patient)
                for match in matches:
                    patient_value = match.value
                    value_map = self._property_map[prop].value_map
                    case_value = case.get_case_property(prop)
                    is_equal = value_map.get(patient_value, patient_value) == case_value
                    yield weight if is_equal else 0

        return sum(weights())

    def find_patients(self, requests, case, case_config):
        """
        Matches cases to patients. Returns a list of patients, each
        with a confidence score >= self.threshold
        """
        from corehq.motech.openmrs.logger import logger
        from corehq.motech.openmrs.repeater_helpers import search_patients

        self._property_map = get_property_map(case_config)

        candidates = {}  # key on OpenMRS UUID to filter duplicates
        for prop in self.searchable_properties:
            value = case.get_case_property(prop)
            if value:
                response_json = search_patients(requests, value)
                for patient in response_json['results']:
                    score = self.get_score(patient, case)
                    if score >= self.threshold:
                        candidates[patient['uuid']] = PatientScore(patient, score)
        if not candidates:
            logger.info(
                'Unable to match case "%s" (%s): No candidate patients found.',
                case.name, case.get_id,
            )
            return []
        if len(candidates) == 1:
            patient = list(candidates.values())[0].patient
            logger.info(
                'Matched case "%s" (%s) to ONLY patient candidate: \n%s',
                case.name, case.get_id, pformat(patient, indent=2),
            )
            return [patient]
        patients_scores = sorted(six.itervalues(candidates), key=lambda candidate: candidate.score, reverse=True)
        if patients_scores[0].score / patients_scores[1].score > 1 + self.confidence_margin:
            # There is more than a `confidence_margin` difference
            # (defaults to 10%) in score between the best-ranked
            # patient and the second-best-ranked patient. Let's go with
            # Patient One.
            patient = patients_scores[0].patient
            logger.info(
                'Matched case "%s" (%s) to BEST patient candidate: \n%s',
                case.name, case.get_id, pformat(patients_scores, indent=2),
            )
            return [patient]
        # We can't be sure. Just send them all.
        logger.info(
            'Unable to match case "%s" (%s) to patient candidates: \n%s',
            case.name, case.get_id, pformat(patients_scores, indent=2),
        )
        return [ps.patient for ps in patients_scores]
