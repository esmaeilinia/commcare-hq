from __future__ import absolute_import

from django.test import SimpleTestCase
from jsonpath_rw import parse

from corehq.motech.openmrs.finders import PatientFinder, WeightedPropertyPatientFinder


PATIENT = {
    'person': {
        'gender': 'F',
        'birthdate': '1984-01-01T00:00:00.000+0200',
        'preferredName': {
            'givenName': 'Mahapajapati',
            'familyName': 'Gotami',
        },
        'preferredAddress': {
            'address1': '56 Barnet Street',
            'address2': 'Gardens',
        },
        'attributes': [
            {
                # caste: Buddhist
                'attributeType': {'uuid': 'c1f4239f-3f10-11e4-adec-0800271c1b75'},
                'value': 'Buddhist',
            },
            {
                # class: sc
                'attributeType': {'uuid': 'c1f455e7-3f10-11e4-adec-0800271c1b75'},
                'value': 'c1fcd1c6-3f10-11e4-adec-0800271c1b75',
            }
        ]
    }
}


class CaseMock(dict):
    def get_case_property(self, prop):
        return self[prop]


class PatientFinderTests(SimpleTestCase):
    """
    Tests PatientFinder.wrap()
    """

    def test_wrap_subclass(self):
        """
        PatientFinder.wrap() should return the type of subclass determined by "doc_type"
        """
        finder = PatientFinder.wrap({
            'doc_type': 'WeightedPropertyPatientFinder',
            'searchable_properties': [],
            'property_weights': [],
        })
        self.assertIsInstance(finder, WeightedPropertyPatientFinder)


class WeightedPropertyPatientFinderTests(SimpleTestCase):
    """
    Tests WeightedPropertyPatientFinder.set_property_map()
    """

    def setUp(self):
        self.case_config = {
            'person_properties': {
                'gender': {'doc_type': 'CaseProperty', 'case_property': 'sex'},
                'birthdate': {'doc_type': 'CaseProperty', 'case_property': 'dob'},
            },
            'person_preferred_name': {
                'givenName': {'doc_type': 'CaseProperty', 'case_property': 'first_name'},
                'familyName': {'doc_type': 'CaseProperty', 'case_property': 'last_name'},
            },
            'person_preferred_address': {
                'address1': {'doc_type': 'CaseProperty', 'case_property': 'address_1'},
                'address2': {'doc_type': 'CaseProperty', 'case_property': 'address_2'},
            },
            'person_attributes': {
                'c1f4239f-3f10-11e4-adec-0800271c1b75': {'doc_type': 'CaseProperty', 'case_property': 'caste'},
                'c1f455e7-3f10-11e4-adec-0800271c1b75': {
                    'doc_type': 'CasePropertyConcept',
                    'case_property': 'class',
                    'value_concepts': {
                        'sc': 'c1fcd1c6-3f10-11e4-adec-0800271c1b75',
                        'general': 'c1fc20ab-3f10-11e4-adec-0800271c1b75',
                        'obc': 'c1fb51cc-3f10-11e4-adec-0800271c1b75',
                        'other_caste': 'c207073d-3f10-11e4-adec-0800271c1b75',
                        'st': 'c20478b6-3f10-11e4-adec-0800271c1b75'
                    }
                },
            },
        }
        self.finder = WeightedPropertyPatientFinder.wrap({
            'doc_type': 'WeightedPropertyPatientFinder',
            'searchable_properties': ['last_name'],
            'property_weights': [
                {"case_property": "first_name", "weight": 0.45},
                {"case_property": "last_name", "weight": 0.45},
                {"case_property": "dob", "weight": 0.2},
                {"case_property": "address_2", "weight": 0.2},
            ],
        })
        self.finder.set_property_map(self.case_config)

    def test_person_properties_jsonpath(self):
        for prop in ('sex', 'dob'):
            matches = self.finder._property_map[prop].jsonpath.find(PATIENT)
            self.assertEqual(len(matches), 1, 'jsonpath "{}" did not uniquely match a patient value'.format(
                self.finder._property_map[prop].jsonpath
            ))
            patient_value = matches[0].value
            self.assertEqual(patient_value, {
                'sex': 'F',
                'dob': '1984-01-01T00:00:00.000+0200',
            }[prop])

    def test_person_preferred_name_jsonpath(self):
        for prop in ('first_name', 'last_name'):
            matches = self.finder._property_map[prop].jsonpath.find(PATIENT)
            self.assertEqual(len(matches), 1, 'jsonpath "{}" did not uniquely match a patient value'.format(
                self.finder._property_map[prop].jsonpath
            ))
            patient_value = matches[0].value
            self.assertEqual(patient_value, {
                'first_name': 'Mahapajapati',
                'last_name': 'Gotami',
            }[prop])

    def test_person_preferred_address_jsonpath(self):
        for prop in ('address_1', 'address_2'):
            matches = self.finder._property_map[prop].jsonpath.find(PATIENT)
            self.assertEqual(len(matches), 1, 'jsonpath "{}" did not uniquely match a patient value'.format(
                self.finder._property_map[prop].jsonpath
            ))
            patient_value = matches[0].value
            self.assertEqual(patient_value, {
                'address_1': '56 Barnet Street',
                'address_2': 'Gardens',
            }[prop])

    def test_person_attributes_jsonpath(self):
        for prop in ('caste', 'class'):
            matches = self.finder._property_map[prop].jsonpath.find(PATIENT)
            self.assertEqual(len(matches), 1, 'jsonpath "{}" did not uniquely match a patient value'.format(
                self.finder._property_map[prop].jsonpath
            ))
            patient_value = matches[0].value
            self.assertEqual(patient_value, {
                'caste': 'Buddhist',
                'class': 'c1fcd1c6-3f10-11e4-adec-0800271c1b75',
            }[prop])

    def test_get_score_ge_1(self):
        case = CaseMock({
            'first_name': 'Mahapajapati',
            'last_name': 'Gotami',
            'address_1': '56 Barnet Street',
            'address_2': 'Gardens',
            'dob': '1984-01-01T00:00:00.000+0200',
        })
        score = self.finder.get_score(PATIENT, case)
        self.assertGreaterEqual(score, 1)

    def test_get_score_lt_1(self):
        case = CaseMock({
            'first_name': 'Mahapajapati',
            'last_name': 'Gotami',
            'address_1': '585 Massachusetts Ave',
            'address_2': 'Cambridge',
            'dob': '1984-12-03T00:00:00.000-0400',
        })
        score = self.finder.get_score(PATIENT, case)
        self.assertLess(score, 1)
