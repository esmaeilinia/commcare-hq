from django.http import Http404
from django.template.context import RequestContext
import simplejson
from corehq.apps.api.es import XFormES
from corehq.apps.fixtures.models import FixtureDataItem, FixtureDataType
from corehq.apps.groups.models import Group
from corehq.apps.reports.datatables import DataTablesColumn, DataTablesHeader
from corehq.apps.reports.generic import GenericTabularReport
from corehq.apps.reports.standard import CustomProjectReport
from dimagi.utils.decorators import inline
from dimagi.utils.decorators.memoized import memoized
from pact.enums import PACT_HP_GROUPNAME, PACT_DOMAIN, PACT_PROVIDER_FIXTURE_TAG
from pact.forms.patient_form import PactPatientForm
from pact.forms.weekly_schedule_form import ScheduleForm
from pact.models import  PactPatientCase
from pact.reports import  PactDrilldownReportMixin, ESSortableMixin
from pact.utils import pact_script_fields


#cloudcare urls:

#pn:     /a/pact/cloudcare/apps/view/0ff529f53c26f44e1fa020e79afe0b1b/0/1/case/%(case_id)s/enter/
#dot:     /a/pact/cloudcare/apps/view/0ff529f53c26f44e1fa020e79afe0b1b/0/2/case/%(case_id)s/enter/
#bw:     /a/pact/cloudcare/apps/view/0ff529f53c26f44e1fa020e79afe0b1b/0/3/case/%(case_id)s/enter/
#address: /a/pact/cloudcare/apps/view/0ff529f53c26f44e1fa020e79afe0b1b/0/4/case/%(case_id)s/enter/


class PactPatientInfoReport(PactDrilldownReportMixin,ESSortableMixin, GenericTabularReport, CustomProjectReport):
    slug = "patient"
    description = "some patient"

    hide_filters = True
    filters = []
    ajax_pagination = True
    xform_es = XFormES()

    default_sort = {
        "received_on": "desc"
    }

    name = "Patient Info"

    @memoized
    def get_case(self):
        return PactPatientCase.get(self.request.GET['patient_id'])

#    @property
#    def name(self):
#        if hasattr(self, 'request'):
#            if self.request.GET.get('patient_id', None) is not None:
#                case = self.get_case()
#                return "Patient Info :: %s" % case.name
#        else:
#            return "Patient Info"

    def patient_submissions_query(self):
        #todo: remove and deprecate - unused
        query = {
            "fields": [
                "form.#type",
                "form.encounter_date",
                "form.note.encounter_date",
                "form.case.case_id",
                "form.case.@case_id",
                "received_on",
                "form.meta.timeStart",
                "form.meta.timeEnd"
            ],
            "filter": {
                "and": [
                    {
                        "term": {
                            "domain.exact": "pact"
                        }
                    },
                    {
                        "query": {
                            "query_string": {
                                "query": "(form.case.case_id:{{ patient_doc.get_id }} OR form.case.@case_id:{{ patient_doc.get_id }})"},
                        }
                    }
                ]
            },
            "sort": {
                "received_on": "desc"
            },
            "size": 10,
            "from": 0
        }


    @property
    def report_context(self):
        patient_doc = self.get_case()
        view_mode = self.request.GET.get('view', 'info')
        ret = RequestContext(self.request)
        ret = {'patient_doc': patient_doc}
        #        ret.update(csrf(self.request))
        ret['pt_root_url'] = PactPatientInfoReport.get_url(
            *[self.request.domain]) + "?patient_id=%s" % self.request.GET['patient_id']
        ret['view_mode'] = view_mode

        if view_mode == 'info':
            self.report_template_path = "pact/patient/pactpatient_info.html"
        elif view_mode == 'submissions':
            tabular_context = super(PactPatientInfoReport, self).report_context
            tabular_context.update(ret)
            self.report_template_path = "pact/patient/pactpatient_submissions.html"
            return tabular_context
        elif view_mode == 'schedule':
#            ret.update(patient_doc.schedules)
            the_form = ScheduleForm()
            ret['schedule_form'] = the_form
            ret['schedule_fields'] = simplejson.dumps(the_form.fields.keys())
            self.report_template_path = "pact/patient/pactpatient_schedule.html"
        elif view_mode == 'edit':
            the_form = PactPatientForm(patient_doc)
            ret['patient_form'] = the_form
            self.report_template_path = "pact/patient/pactpatient_edit.html"
        elif view_mode == 'providers':


            self.report_template_path = "pact/patient/pactpatient_providers.html"
        else:
            raise Http404
        return ret


    @property
    def headers(self):
        return DataTablesHeader(DataTablesColumn("Form", prop_name="form.#type", span=1),
                                DataTablesColumn("CHW", prop_name="form.meta.username", span=2),
                                DataTablesColumn("Created Date", prop_name="form.meta.timeStart", span=2),
                                DataTablesColumn("Received", prop_name="received_on", span=2),
                                DataTablesColumn("Encounter Date", sortable=False, span=2),
        )

    @property
    @memoized
    def es_results(self):
        if not self.request.GET.has_key('patient_id'):
            return None

        full_query = {
            'query': {
                "filtered": {
                    "query": {
                        "query_string": {
                            "query": "(form.case.case_id:%(case_id)s OR form.case.@case_id:%(case_id)s)" % dict(
                                case_id=self.request.GET['patient_id'])
                        }
                    },
                    "filter": {
                        "and": [
                            {
                                "term": {
                                    "domain.exact": self.request.domain
                                }
                            }
                        ]
                    }
                }
            },
            "fields": [
                "form.#type",
#                "form.encounter_date",
#                "form.note.encounter_date",
#                "form.case.case_id",
#                "form.case.@case_id",
#                "form.pact_id",
#                "form.note.pact_id",
                "received_on",
                "form.meta.timeStart",
                "form.meta.timeEnd",
                "form.meta.username"
            ],
            "sort": self.get_sorting_block(),
            "size": self.pagination.count,
            "from": self.pagination.start

        }

        full_query['script_fields'] = pact_script_fields()
        return self.xform_es.run_query(full_query)


    @property
    def rows(self):
        """
            Override this method to create a functional tabular report.
            Returns 2D list of rows.
            [['row1'],[row2']]
        """
        if self.request.GET.has_key('patient_id'):
            rows = []

            def _format_row(row_field_dict):
                yield row_field_dict["form.#type"].replace('_', ' ').title()
                yield row_field_dict.get("form.meta.username", "")
                yield self.format_date(row_field_dict.get("form.meta.timeStart", ""))
                yield self.format_date(row_field_dict["received_on"])
                if row_field_dict["script_encounter_date"] != None:
                    yield row_field_dict["script_encounter_date"]
                else:
                    yield "---"

            res = self.es_results
            if res.has_key('error'):
                pass
            else:
                for result in res['hits']['hits']:
                    yield list(_format_row(result['fields']))



