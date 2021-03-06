# coding=utf-8
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals
import sqlalchemy
from sqlagg.base import AliasColumn, QueryMeta, CustomQueryColumn, TableNotFoundException
from sqlagg.columns import SumColumn, MaxColumn, SimpleColumn, CountColumn, CountUniqueColumn, MeanColumn, \
    MonthColumn
from collections import defaultdict
from corehq.apps.locations.models import SQLLocation, get_location
from corehq.apps.products.models import SQLProduct

from corehq.apps.reports.datatables import DataTablesColumn, DataTablesHeader, DataTablesColumnGroup
from corehq.apps.reports.sqlreport import DataFormatter, \
    TableDataFormat, calculate_total_row
from corehq.apps.userreports.util import get_table_name
from custom.intrahealth.report_calcs import _locations_per_type
from custom.intrahealth.utils import YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR, \
    YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR_PER_PRODUCT, YEKSI_NAA_REPORTS_LOGISTICIEN, \
    YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR_PER_PROGRAM, COMMANDE_V1, COMMANDE_V2, OPERATEUR_V1, OPERATEUR_V2
from dateutil.rrule import rrule, MONTHLY
from dateutil.relativedelta import relativedelta
from django.utils.functional import cached_property
from sqlagg.filters import EQ, BETWEEN, AND, GTE, LTE, NOT, IN, SqlFilter, get_column, OR
from corehq.apps.reports.sqlreport import DatabaseColumn, SqlData, AggregateColumn
from django.utils.translation import ugettext as _
from sqlalchemy import select
from corehq.apps.reports.util import get_INFilter_bindparams
from custom.utils.utils import clean_IN_filter_value
from memoized import memoized
from dimagi.utils.parsing import json_format_date
import six
from functools import reduce
from six.moves import range

PRODUCT_NAMES = {
    'diu': ["diu"],
    'jadelle': ["jadelle"],
    'depo-provera': ["d\xe9po-provera", "depo-provera"],
    'd\xe9po-provera': ["d\xe9po-provera", "depo-provera"],
    'microlut/ovrette': ["microlut/ovrette"],
    'microgynon/lof.': ["microgynon/lof."],
    'preservatif masculin': ["pr\xe9servatif masculin", "preservatif masculin"],
    'preservatif feminin': ["pr\xe9servatif f\xe9minin", "preservatif feminin"],
    'cu': ["cu"],
    'collier': ["collier"]
}


def _locations_filter(archived_locations):
    return NOT(IN('location_id', get_INFilter_bindparams('archived_locations', archived_locations)))


class BaseSqlData(SqlData):
    datatables = False
    show_charts = False
    show_total = True
    custom_total_calculate = False
    no_value = {'sort_key': 0, 'html': 0}
    fix_left_col = False

    def percent_fn(self, x, y):
        return "%(p).2f%%" % \
               {
                   "p": (100 * float(y or 0) / float(x or 1))
               }

    def format_data_and_cast_to_float(self, value):
        return {"html": round(value, 2), "sort_key": round(value, 2)} if value is not None else value

    @property
    def filters(self):
        filters = [BETWEEN("date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        return filters

    @property
    def group_by(self):
        return []

    @property
    def rows(self):
        formatter = DataFormatter(TableDataFormat(self.columns, no_value=self.no_value))
        rows = list(formatter.format(self.data, keys=self.keys, group_by=self.group_by))
        return rows

    # this is copy/paste from the
    # https://github.com/dimagi/commcare-hq/blob/master/corehq/apps/reports/sqlreport.py#L383
    # we added possibility to sum Float values
    def calculate_total_row(self, rows):
        total_row = []
        if len(rows) > 0:
            num_cols = len(rows[0])
            for i in range(num_cols):
                colrows = [cr[i] for cr in rows if isinstance(cr[i], dict)]
                columns = [r.get('sort_key') for r in colrows if
                           isinstance(r.get('sort_key'), six.integer_types + (float,))]
                if len(columns):
                    total_row.append(reduce(lambda x, y: x + y, columns, 0))
                else:
                    total_row.append('')
        return total_row

    @property
    def filter_values(self):
        return clean_IN_filter_value(super(BaseSqlData, self).filter_values, 'archived_locations')


class ConventureData(BaseSqlData):
    slug = 'conventure'
    title = 'Converture'
    show_total = False
    table_name = 'fluff_CouvertureFluff'
    custom_total_calculate = True

    @property
    def filters(self):
        # We have to filter data by real_date_repeat not date(first position in filters list).
        # Filtering is done directly in columns method(CountUniqueColumn).
        filters = super(ConventureData, self).filters
        filters.append(AND([GTE('real_date_repeat', "strsd"), LTE('real_date_repeat', "stred")]))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters[1:]

    @property
    def group_by(self):
        group_by = []

        if 'region_id' in self.config:
            group_by.append('region_id')
        elif 'district_id' in self.config:
            group_by.append('district_id')

        if self.config['startdate'].month != self.config['enddate'].month:
            group_by.append('month')
        return group_by

    @property
    def columns(self):
        registered_column = "registered_total_for_region"
        if 'district_id' in self.config:
            registered_column = 'registered_total_for_district'
        columns = [
            DatabaseColumn("No de PPS (number of PPS registered in that region)",
                           MaxColumn(registered_column, alias='registered')),
            DatabaseColumn("No de PPS planifie (number of PPS planned)", MaxColumn('planned_total')),
            DatabaseColumn("No de PPS avec livrasion cet mois (number of PPS visited this month)",
                           CountUniqueColumn('location_id', alias="visited")
                           ),
            AggregateColumn("Taux de couverture (coverage ratio)", self.percent_fn,
                            [AliasColumn('registered'), AliasColumn("visited")]),
            DatabaseColumn("No de PPS avec donnees soumises (number of PPS which submitted data)",
                           CountUniqueColumn('location_id', alias="submitted")
                           ),
            AggregateColumn("Exhaustivite des donnees", self.percent_fn,
                            [AliasColumn('visited'), AliasColumn('submitted')]),
        ]
        if self.config['startdate'].month != self.config['enddate'].month:
            columns.insert(0, DatabaseColumn('Mois', SimpleColumn('month')))
            self.show_total = True
        return columns

    @property
    def rows(self):
        formatter = DataFormatter(TableDataFormat(self.columns, no_value=self.no_value))
        rows = list(formatter.format(self.data, keys=self.keys, group_by=self.group_by))

        # Months are displayed in chronological order
        if 'month' in self.group_by:
            from custom.intrahealth.reports.utils import get_localized_months
            return sorted(rows, key=lambda row: get_localized_months().index(row[0]))

        return rows

    def calculate_total_row(self, rows):
        total_row = super(ConventureData, self).calculate_total_row(rows)
        if len(total_row) != 0:
            # two cell's are recalculated because the summation of percentage gives us bad values
            total_row[4] = "%.0f%%" % (total_row[3] * 100 / float(total_row[1]))
            total_row[-1] = "%.0f%%" % (total_row[5] * 100 / float(total_row[3]))
        return total_row


class DispDesProducts(BaseSqlData):
    slug = 'products'
    title = 'Taux de satisfaction de la commande de l\'operateur'
    table_name = 'fluff_TauxDeSatisfactionFluff'
    show_total = False

    @property
    def group_by(self):
        group_by = []
        if 'region_id' in self.config:
            group_by.append('region_id')
        elif 'district_id' in self.config:
            group_by.append('district_id')
        group_by.append('product_id')
        return group_by

    @property
    @memoized
    def products(self):
        return list(SQLProduct.objects.filter(domain=self.config['domain'], is_archived=False).order_by('name'))

    @property
    def rows(self):
        products = self.products
        values = {
            product.product_id: [0, 0, 0]
            for product in products
        }

        rows = self.get_data()
        for row in rows:
            product_id = row['product_id']
            values[product_id] = [
                row['commandes_total'],
                row['recus_total'],
                "%d%%" % (100 * row['recus_total']['html'] / (row['commandes_total']['html'] or 1))
            ]

        commandes = ['Commandes']
        raux = ['Raux']
        taux = ['Taux']

        for product in products:
            values_for_product = values[product.product_id]
            commandes.append(values_for_product[0])
            raux.append(values_for_product[1])
            taux.append(values_for_product[2])

        return [commandes, raux, taux]

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('Quantity'))
        for product in self.products:
            headers.add_column(DataTablesColumn(product.name))
        return headers

    @property
    def columns(self):
        return [
            DatabaseColumn('Product Name', SimpleColumn('product_id')),
            DatabaseColumn("Commandes", SumColumn('commandes_total')),
            DatabaseColumn("Recu", SumColumn('recus_total'))
        ]


class TauxDeRuptures(BaseSqlData):
    slug = 'taux_de_ruptures'
    title = 'Disponibilité des Produits - Taux des Ruptures de Stock'
    table_name = 'fluff_IntraHealthFluff'
    col_names = ['total_stock_total']
    have_groups = False
    custom_total_calculate = True

    @property
    def group_by(self):
        group_by = ['product_id']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')

        return group_by

    @property
    def filters(self):
        filter = super(TauxDeRuptures, self).filters
        filter.append(EQ("total_stock_total", "zero"))
        if 'archived_locations' in self.config:
            filter.append(_locations_filter(self.config['archived_locations']))
        return filter

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(
            _("Stock total"),
            CountColumn('total_stock_total'),
            format_fn=lambda x: 1 if x > 0 else 0
        ))
        return columns

    def calculate_total_row(self, rows):
        conventure = ConventureData(self.config)
        if self.config['startdate'].month != self.config['enddate'].month:
            conventure_data_rows = conventure.calculate_total_row(conventure.rows)
            total = conventure_data_rows[3] if conventure_data_rows else 0
        else:
            conventure_data_rows = conventure.rows
            total = conventure_data_rows[0][2]["html"] if conventure_data_rows else 0

        def get_value(x):
            """x can be a value or a sort_key/html dict"""
            return x["sort_key"] if isinstance(x, dict) else x

        for row in rows:
            value = 1 if any(get_value(x) for x in row[1:]) else 0
            row.append({'sort_key': value, 'html': value})

        total_row = list(calculate_total_row(rows))

        taux_rapture_row = ["(%s/%s) %s" % (x, total, self.percent_fn(total, x)) for x in total_row]

        if total_row:
            total_row[0] = 'Total'

        if taux_rapture_row:
            taux_rapture_row[0] = 'Taux rupture'

        rows.append(total_row)

        return taux_rapture_row


class FicheData(BaseSqlData):
    title = ''
    table_name = 'fluff_IntraHealthFluff'
    show_total = True
    col_names = ['actual_consumption', 'billed_consumption', 'consommation-non-facturable']
    have_groups = True

    @property
    def filters(self):
        filters = super(FicheData, self).filters
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        return ['product_id', 'PPS_name']

    @property
    def columns(self):
        diff = lambda x, y: (x or 0) - (y or 0)
        return [
            DatabaseColumn(_("LISTE des PPS"), SimpleColumn('PPS_name')),
            DatabaseColumn(_("Consommation Reelle"),
                           SumColumn('actual_consumption_total', alias='actual_consumption')),
            DatabaseColumn(_("Consommation Facturable"),
                           SumColumn('billed_consumption_total', alias='billed_consumption')),
            AggregateColumn(_("Consommation Non Facturable"), diff,
                            [AliasColumn('actual_consumption'), AliasColumn('billed_consumption')]),
        ]


class PPSAvecDonnees(BaseSqlData):
    slug = 'pps_avec_donnees'
    title = 'PPS Avec Données'
    table_name = 'fluff_CouvertureFluff'
    col_names = ['location_id']
    have_groups = False

    @property
    def filters(self):
        filters = super(PPSAvecDonnees, self).filters
        filters.append(AND([GTE('real_date_repeat', "strsd"), LTE('real_date_repeat', "stred")]))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters[1:]

    @property
    def group_by(self):
        group_by = []
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('pps_name')
        return group_by

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('pps_name')))

        columns.append(DatabaseColumn(_("PPS Avec Données Soumises"),
                                      CountUniqueAndSumCustomColumn('location_id'),
                                      format_fn=lambda x: {'sort_key': int(x), 'html': int(x)})
                       )
        return columns

    @property
    def rows(self):
        rows = super(PPSAvecDonnees, self).rows
        if 'district_id' in self.config:
            locations_included = [row[0] for row in rows]
        else:
            return rows
        all_locations = SQLLocation.objects.get(
            location_id=self.config['district_id']
        ).get_children().exclude(is_archived=True).values_list('name', flat=True)
        locations_not_included = set(all_locations) - set(locations_included)
        return rows + [[location, {'sort_key': 0, 'html': 0}] for location in locations_not_included]


class DateSource(BaseSqlData):
    title = ''
    table_name = 'fluff_RecapPassageFluff'

    @property
    def filters(self):
        filters = super(DateSource, self).filters
        if 'location_id' in self.config:
            filters.append(EQ('location_id', 'location_id'))
        filters.append(NOT(EQ('product_id', 'empty_prd_code')))
        return filters

    @property
    def group_by(self):
        return ['date', ]

    @property
    def columns(self):
        return [
            DatabaseColumn(_("Date"), SimpleColumn('date')),
        ]


class RecapPassageData(BaseSqlData):
    title = ''
    table_name = 'fluff_RecapPassageFluff'
    datatables = True
    show_total = False
    fix_left_col = True
    have_groups = False

    @property
    def slug(self):
        return 'recap_passage_%s' % json_format_date(self.config['startdate'])

    @property
    def title(self):
        return 'Recap Passage %s' % json_format_date(self.config['startdate'])

    @property
    def filters(self):
        filters = super(RecapPassageData, self).filters
        if 'location_id' in self.config:
            filters.append(EQ("location_id", "location_id"))
        filters.append(NOT(EQ('product_id', 'empty_prd_code')))
        return filters

    @property
    def group_by(self):
        return ['date', 'product_id']

    @property
    def columns(self):
        diff = lambda x, y: (x or 0) - (y or 0)

        def get_prd_name(id):
            try:
                return SQLProduct.objects.get(product_id=id, domain=self.config['domain'],
                                              is_archived=False).name
            except SQLProduct.DoesNotExist:
                pass

        return [
            DatabaseColumn(_("Designations"), SimpleColumn('product_id'),
                           format_fn=lambda id: get_prd_name(id)),
            DatabaseColumn(_("Stock apres derniere livraison"), SumColumn('product_old_stock_total')),
            DatabaseColumn(_("Stock disponible et utilisable a la livraison"), SumColumn('product_total_stock')),
            DatabaseColumn(_("Livraison"), SumColumn('product_livraison')),
            DatabaseColumn(_("Stock Total"), SumColumn('product_display_total_stock', alias='stock_total')),
            DatabaseColumn(_("Precedent"), SumColumn('product_old_stock_pps')),
            DatabaseColumn(_("Recu hors entrepots mobiles"), SumColumn('product_outside_receipts_amount')),
            AggregateColumn(_("Non Facturable"), diff,
                            [AliasColumn('aconsumption'), AliasColumn("bconsumption")]),
            DatabaseColumn(_("Facturable"), SumColumn('product_billed_consumption', alias='bconsumption')),
            DatabaseColumn(_("Reelle"), SumColumn('product_actual_consumption', alias='aconsumption')),
            DatabaseColumn("Stock Total", AliasColumn('stock_total')),
            DatabaseColumn("PPS Restant", SumColumn('product_pps_restant')),
            DatabaseColumn("Pertes et Adjustement", SumColumn('product_loss_amt'))
        ]


class ConsommationData(BaseSqlData):
    slug = 'consommation'
    title = 'Consommation'
    table_name = 'fluff_IntraHealthFluff'
    show_charts = True
    chart_x_label = 'Products'
    chart_y_label = 'Number of consumption'
    datatables = True
    fix_left_col = True
    col_names = ['actual_consumption_total']
    have_groups = False

    @property
    def filters(self):
        filters = super(ConsommationData, self).filters
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = ['product_id']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')

        return group_by

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(_("Consumption"), SumColumn('actual_consumption_total')))
        return columns


class TauxConsommationData(BaseSqlData):
    slug = 'taux_consommation'
    title = 'Taux de Consommation'
    table_name = 'fluff_IntraHealthFluff'
    datatables = True
    custom_total_calculate = True
    fix_left_col = True
    col_names = ['consumption', 'stock', 'taux-consommation']
    sum_cols = ['consumption', 'stock']
    have_groups = True

    @property
    def filters(self):
        filters = super(TauxConsommationData, self).filters
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = []
        if 'region_id' in self.config:
            group_by.extend(['district_name', 'PPS_name'])
        else:
            group_by.append('PPS_name')
        group_by.append('product_id')
        return group_by

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(_("Consommation reelle"),
                                      MeanColumn('actual_consumption_total', alias="consumption"),
                                      format_fn=self.format_data_and_cast_to_float))
        columns.append(DatabaseColumn(_("Stock apres derniere livraison"),
                                      MeanColumn('stock_total', alias="stock"),
                                      format_fn=self.format_data_and_cast_to_float))
        columns.append(AggregateColumn(_("Taux consommation"), self.percent_fn,
                                       [AliasColumn('stock'), AliasColumn('consumption')],
                                       slug='taux-consommation'))
        return columns

    def calculate_total_row(self, rows):
        total_row = []
        if len(rows) > 0:
            num_cols = len(rows[0])
            for i in range(num_cols):
                if i != 0 and i % 3 == 0:
                    cp = total_row[-2:]
                    total_row.append("%s%%" % (100 * int(cp[0] or 0) // (cp[1] or 1)))
                else:
                    colrows = [cr[i] for cr in rows if isinstance(cr[i], dict)]
                    columns = [
                        r.get('sort_key')
                        for r in colrows
                        if isinstance(r.get('sort_key'), six.integer_types)
                    ]
                    if len(columns):
                        total_row.append(reduce(lambda x, y: x + y, columns, 0))
                    else:
                        total_row.append('')

        return total_row


class NombreData(BaseSqlData):
    slug = 'nombre'
    title = 'Nombre de mois de stock disponibles et utilisables aux PPS'
    table_name = 'fluff_IntraHealthFluff'
    datatables = True
    custom_total_calculate = True
    fix_left_col = True
    col_names = ['quantity', 'cmm', 'nombre-mois-stock-disponible-et-utilisable']
    sum_cols = ['quantity', 'cmm']
    have_groups = True

    @property
    def filters(self):
        filters = super(NombreData, self).filters
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = []
        if 'region_id' in self.config:
            group_by.extend(['district_name', 'PPS_name'])
        else:
            group_by.append('PPS_name')
        group_by.append('product_id')

        return group_by

    @property
    def columns(self):
        div = lambda x, y: "%0.3f" % (float(x) / (float(y) or 1.0))
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(_("Quantite produits entreposes au PPS"),
                                      MeanColumn('quantity_total', alias="quantity"),
                                      format_fn=self.format_data_and_cast_to_float))
        columns.append(DatabaseColumn(_("CMM"), MeanColumn('cmm_total', alias="cmm"),
                                      format_fn=self.format_data_and_cast_to_float))
        columns.append(AggregateColumn(_("Nombre mois stock disponible et utilisable"), div,
                                       [AliasColumn('quantity'), AliasColumn('cmm')]))
        return columns

    def calculate_total_row(self, rows):
        total_row = []
        if len(rows) > 0:
            num_cols = len(rows[0])
            for i in range(num_cols):
                if i != 0 and i % 3 == 0:
                    cp = total_row[-2:]
                    total_row.append("%0.3f" % (float(cp[0]) / (float(cp[1]) or 1.0)))
                else:
                    colrows = [cr[i] for cr in rows if isinstance(cr[i], dict)]
                    columns = [r.get('sort_key') for r in colrows if
                               isinstance(r.get('sort_key'), six.integer_types + (float,))]
                    if len(columns):
                        total_row.append(reduce(lambda x, y: x + y, columns, 0))
                    else:
                        total_row.append('')

        return total_row


class GestionDeLIPMTauxDeRuptures(TauxDeRuptures):
    table_name = 'fluff_TauxDeRuptureFluff'
    title = 'Gestion de l`IPM - Taux des Ruptures de Stock'

    @property
    def filters(self):
        return super(TauxDeRuptures, self).filters

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(_("Stock total"), SumColumn('total_stock_total')))
        return columns


class DureeData(BaseSqlData):
    slug = 'duree'
    custom_total_calculate = True
    title = 'Durée moyenne des retards de livraison'
    table_name = 'fluff_LivraisonFluff'
    have_groups = False
    col_names = ['duree_moyenne_livraison_total']

    @property
    def group_by(self):
        return ['district_name']

    @property
    def columns(self):
        columns = [DatabaseColumn(_("District"), SimpleColumn('district_name')),
                   DatabaseColumn(_("Retards de livraison (jours)"),
                                  SumAndAvgGCustomColumn('duree_moyenne_livraison_total'),
                                  format_fn=lambda x: {'sort_key': float(x), 'html': float(x)})]
        return columns

    def calculate_total_row(self, rows):
        total_row = super(DureeData, self).calculate_total_row(rows)
        if total_row:
            total_row[0] = 'Moyenne Region'
            total_row[-1] = "%.2f" % (total_row[-1] / float(len(self.rows)))
        return total_row


class RecouvrementDesCouts(BaseSqlData):
    slug = 'recouvrement'
    custom_total_calculate = True
    title = 'Recouvrement des côuts - Taxu de Recouvrement'

    table_name = 'fluff_RecouvrementFluff'
    have_groups = False
    col_names = ['district_name', 'payments_amount_paid', 'payments_amount_to_pay',
                 'payments_in_30_days', 'payments_in_3_months', 'payments_in_year']

    @property
    def group_by(self):
        return ['district_name']

    @property
    def filters(self):
        filters = super(RecouvrementDesCouts, self).filters
        return filters

    @property
    def columns(self):
        columns = [DatabaseColumn(_("District"), SimpleColumn('district_name'))]
        columns.append(DatabaseColumn(_("Montant dû"), SumColumn('payments_amount_to_pay')))
        columns.append(DatabaseColumn(_("Montant payé"), SumColumn('payments_amount_paid')))
        columns.append(DatabaseColumn(_("Payé dans le 30 jours"), SumColumn('payments_in_30_days')))
        columns.append(DatabaseColumn(_("Payé dans le 3 mois"), SumColumn('payments_in_3_months')))
        columns.append(DatabaseColumn(_("Payé dans l`annèe"), SumColumn('payments_in_year')))
        return columns

    def calculate_total_row(self, rows):
        total_row = super(RecouvrementDesCouts, self).calculate_total_row(rows)
        if total_row:
            total_row[0] = 'Total Region'
        return total_row


class IntraHealthQueryMeta(QueryMeta):

    def __init__(self, table_name, filters, group_by, key):
        self.key = key
        super(IntraHealthQueryMeta, self).__init__(table_name, filters, group_by, [])
        assert len(filters) > 0
        self.filter = AND(self.filters) if len(self.filters) > 1 else self.filters[0]

    def execute(self, metadata, connection, filter_values):
        try:
            table = metadata.tables[self.table_name]
        except KeyError:
            raise TableNotFoundException("Unable to query table, table not found: %s" % self.table_name)
        return connection.execute(self._build_query(table, filter_values)).fetchall()

    def _build_query(self, table, filter_values):
        raise NotImplementedError()


class SumAndAvgQueryMeta(IntraHealthQueryMeta):

    def _build_query(self, table, filter_values):
        key_column = table.c[self.key]
        sum_query = sqlalchemy.alias(
            sqlalchemy.select(
                self.group_by + [sqlalchemy.func.sum(key_column).label('sum_col')] + [table.c.month],
                group_by=self.group_by + [table.c.month],
                whereclause=self.filter.build_expression(table),
            ), name='s')

        return select(
            self.group_by + [sqlalchemy.func.avg(sum_query.c.sum_col).label(self.key)],
            group_by=self.group_by,
            from_obj=sum_query
        ).params(filter_values)


class CountUniqueAndSumQueryMeta(IntraHealthQueryMeta):

    def _build_query(self, table, filter_values):
        key_column = table.c[self.key]
        subquery = sqlalchemy.alias(
            sqlalchemy.select(
                self.group_by + [sqlalchemy.func.count(sqlalchemy.distinct(key_column)).label('count_unique')],
                group_by=self.group_by + [table.c.month],
                whereclause=self.filter.build_expression(table),
            ),
            name='cq')

        return sqlalchemy.select(
            self.group_by + [sqlalchemy.func.sum(subquery.c.count_unique).label(self.key)],
            group_by=self.group_by,
            from_obj=subquery
        ).params(filter_values)


class IntraHealthCustomColumn(CustomQueryColumn):

    def get_query_meta(self, default_table_name, default_filters, default_group_by, default_order_by):
        table_name = self.table_name or default_table_name
        filters = self.filters or default_filters
        group_by = self.group_by or default_group_by
        return self.query_cls(table_name, filters, group_by, self.key)


class SumAndAvgGCustomColumn(IntraHealthCustomColumn):
    query_cls = SumAndAvgQueryMeta
    name = 'sum_and_avg'


class CountUniqueAndSumCustomColumn(IntraHealthCustomColumn):
    query_cls = CountUniqueAndSumQueryMeta
    name = 'count_unique_and_sum'


class IntraHealthSqlData(SqlData):
    datatables = False
    show_charts = False
    show_total = False
    custom_total_calculate = False
    no_value = {'sort_key': 0, 'html': 0}
    fix_left_col = False

    def percent_fn(self, x, y):
        return "%(p).2f%%" % \
            {
                "p": (100 * float(y or 0) / float(x or 1))
            }

    def format_data_and_cast_to_float(self, value):
        return {"html": "%.2f".format(value), "sort_key": "%.2f".format(value)} if value is not None else value

    @property
    def engine_id(self):
        return 'ucr'

    @property
    def filters(self):
        filters = [BETWEEN("date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        return filters

    @property
    def group_by(self):
        return []

    @property
    def rows(self):
        formatter = DataFormatter(TableDataFormat(self.columns, no_value=self.no_value))
        rows = list(formatter.format(self.data, keys=self.keys, group_by=self.group_by))
        return rows

    @property
    def filter_values(self):
        return clean_IN_filter_value(super(IntraHealthSqlData, self).filter_values, 'archived_locations')


class TauxConsommationDataSourceMixin(IntraHealthSqlData):
    title = ''

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = ['doc_id', 'product_name', 'actual_consumption', 'total_stock']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')
        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn(_("Product Name"), SimpleColumn('product_name')),
            DatabaseColumn(_("Consommation reelle"), SimpleColumn('actual_consumption')),
            DatabaseColumn(_("Stock apres derniere livraison"), SimpleColumn('total_stock')),
        ]
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))
        return columns


class TauxConsommationDataV1DataSource(TauxConsommationDataSourceMixin):
    slug = 'TauxConsommationDataV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class TauxConsommationDataV2DataSource(TauxConsommationDataSourceMixin):
    slug = 'TauxConsommationDataV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class TauxConsommationData2(IntraHealthSqlData):
    show_total = False
    slug = 'taux_consommation'
    title = 'Taux de Consommation'
    product_names = set()
    loc_names = set()

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        rows_v1 = TauxConsommationDataV1DataSource(config=self.config).get_data()
        rows_v2 = TauxConsommationDataV2DataSource(config=self.config).get_data()

        if 'region_id' in self.config:
            loc_name = 'district_name'
        else:
            loc_name = 'PPS_name'

        data = {}
        for row in rows_v1:
            if row['product_name'] and row[loc_name]:
                self.loc_names.add(row[loc_name])
                self.product_names.add(row['product_name'])
                if row[loc_name] not in data:
                    data[row[loc_name]] = {}
                if row['product_name'] not in data[row[loc_name]]:
                    data[row[loc_name]][row['product_name']] = defaultdict(list)
                data[row[loc_name]][row['product_name']]['actual_consumption'].append(row['actual_consumption'])
                data[row[loc_name]][row['product_name']]['total_stock'].append(row['total_stock'])
        for row in rows_v2:
            if row['product_name'] and row[loc_name]:
                self.loc_names.add(row[loc_name])
                self.product_names.add(row['product_name'])
                if row[loc_name] not in data:
                    data[row[loc_name]] = {}
                if row['product_name'] not in data[row[loc_name]]:
                    data[row[loc_name]][row['product_name']] = defaultdict(list)
                data[row[loc_name]][row['product_name']]['actual_consumption'].append(row['actual_consumption'])
                data[row[loc_name]][row['product_name']]['total_stock'].append(row['total_stock'])

        self.loc_names = sorted(self.loc_names)
        self.product_names = sorted(self.product_names)
        rows = []
        for loc_name in self.loc_names:
            row = [loc_name]
            for product_name in self.product_names:
                actual_consumption = 0
                total_stock = 0
                if data.get(loc_name) and data[loc_name].get(product_name):
                    actual_consumption = [
                        value for value in data[loc_name][product_name]['actual_consumption'] if value is not None
                    ]
                    total_stock = [
                        value for value in data[loc_name][product_name]['total_stock'] if value is not None
                    ]
                if actual_consumption:
                    actual_consumption = sum(actual_consumption) / len(actual_consumption)
                if total_stock:
                    total_stock = sum(total_stock) / len(total_stock)
                row.append(self.format_data_and_cast_to_float(actual_consumption))
                row.append(self.format_data_and_cast_to_float(total_stock))
                row.append(self.percent_fn(total_stock, actual_consumption))
            rows.append(row)
        return rows

    @property
    def headers(self):
        headers = DataTablesHeader()
        if 'region_id' in self.config:
            headers.add_column(DataTablesColumnGroup('', DataTablesColumn('District')))
        else:
            headers.add_column(DataTablesColumnGroup('', DataTablesColumn('PPS')))

        for product_name in self.product_names:
            headers.add_column(DataTablesColumnGroup(
                product_name,
                DataTablesColumn('Consommation reelle'),
                DataTablesColumn('Stock apres derniere livraison'),
                DataTablesColumn('Taux consommation')
            ))

        return headers


class TauxDeRupturesDataSourceMixin(IntraHealthSqlData):
    title = ''

    @property
    def group_by(self):
        group_by = ['product_name']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')
        return group_by

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def columns(self):
        columns = [
            DatabaseColumn(_("Product Name"), SimpleColumn('product_name')),
        ]
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(
            _("Stock total"), CountColumn('total_stock'), format_fn=lambda x: 1 if x > 0 else 0
        ))
        return columns


class TauxDeRupturesDataV1DataSource(TauxDeRupturesDataSourceMixin):
    slug = 'TauxDeRupturesDataV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class TauxDeRupturesDataV2DataSource(TauxDeRupturesDataSourceMixin):
    slug = 'TauxDeRupturesDataV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class TauxDeRuptures2(IntraHealthSqlData):
    show_total = False
    slug = 'taux_de_ruptures2'
    title = 'Disponibilité des Produits - Taux des Ruptures de Stock'
    product_names = set()

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        rows_v1 = TauxDeRupturesDataV1DataSource(config=self.config).get_data()
        rows_v2 = TauxDeRupturesDataV2DataSource(config=self.config).get_data()

        if 'region_id' in self.config:
            loc_name = 'district_name'
        else:
            loc_name = 'PPS_name'

        data = {}
        for row in rows_v1:
            if row['product_name']:
                self.product_names.add(row['product_name'])
                if row[loc_name] not in data:
                    data[row[loc_name]] = defaultdict(int)
                data[row[loc_name]][row['product_name']] += row['total_stock']
        for row in rows_v2:
            if row['product_name']:
                self.product_names.add(row['product_name'])
                if row[loc_name] not in data:
                    data[row[loc_name]] = defaultdict(int)
                data[row[loc_name]][row['product_name']] += row['total_stock']

        self.product_names = sorted(self.product_names)
        rows = []

        for loc_name, values in data.items():
            row = [loc_name]
            for product_name in self.product_names:
                row.append(values[product_name])
            rows.append(row)
        return rows

    @property
    def headers(self):
        headers = DataTablesHeader()
        if 'region_id' in self.config:
            headers.add_column(
                DataTablesColumn('District')
            )
        else:
            headers.add_column(
                DataTablesColumn('PPS')
            )
        for product_name in self.product_names:
            headers.add_column(
                DataTablesColumn(product_name)
            )
        return headers


class RecapPassageDateDataSourceMixin(IntraHealthSqlData):
    title = ''

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'location_id' in self.config:
            filters.append(EQ("location_id", "location_id"))
        return filters

    @property
    def group_by(self):
        return ['real_date', ]

    @property
    def columns(self):
        return [
            DatabaseColumn(_("Date"), SimpleColumn('real_date')),
        ]


class RecapPassageDateV1DataSource(RecapPassageDateDataSourceMixin):
    slug = 'RecapPassageDateV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class RecapPassageDateV2DataSource(RecapPassageDateDataSourceMixin):
    slug = 'RecapPassageDateV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class DateSource2(IntraHealthSqlData):
    slug = 'dateSource2'
    title = 'DateSource2'
    show_total = False

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        rows_v1 = RecapPassageDateV1DataSource(config=self.config).get_data()
        rows_v2 = RecapPassageDateV2DataSource(config=self.config).get_data()
        return sorted(list(
            set(row['real_date'] for row in rows_v1).union(set(row['real_date'] for row in rows_v2))
        ))

    @property
    def headers(self):
        return []


class RecapPassageDataSourceMixin(IntraHealthSqlData):
    slug = 'recap_passage_data_source'
    show_total = False
    title = 'Recap Passage Data Source'

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'location_id' in self.config:
            filters.append(EQ("location_id", "location_id"))
        return filters

    @property
    def group_by(self):
        return ['real_date', 'product_name']

    @property
    def columns(self):
        return [
            DatabaseColumn(_("Designations"), SimpleColumn('product_name')),
            DatabaseColumn(_("Stock apres derniere livraison"), SumColumn('old_stock_total')),
            DatabaseColumn(_("Stock disponible et utilisable a la livraison"), SumColumn('total_stock')),
            DatabaseColumn(_("Stock Total"), SumColumn('display_total_stock', alias='stock_total')),
            DatabaseColumn(_("Precedent"), SumColumn('old_stock_pps')),
            DatabaseColumn(_("Recu hors entrepots mobiles"), SumColumn('outside_receipts_amt')),
            DatabaseColumn(_("Facturable"), SumColumn('billed_consumption')),
            DatabaseColumn(_("Reelle"), SumColumn('actual_consumption')),
            DatabaseColumn("Stock Total", AliasColumn('stock_total')),
            DatabaseColumn("PPS Restant", SumColumn('pps_stock')),
            DatabaseColumn("Pertes et Adjustement", SumColumn('loss_amt'))
        ]


class RecapPassageV1DataSource(RecapPassageDataSourceMixin):
    slug = 'RecapPassageV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)

    @property
    def columns(self):
        columns = super(RecapPassageV1DataSource, self).columns
        columns.append(
            DatabaseColumn(_("Livraison"), SumColumn('quantity')),
        )
        return columns


class RecapPassageV2DataSource(RecapPassageDataSourceMixin):
    slug = 'RecapPassageV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class RecapPassageData2(IntraHealthSqlData):
    show_total = False

    @property
    def slug(self):
        return 'recap_passage_%s' % json_format_date(self.config['startdate'])

    @property
    def title(self):
        return 'Recap Passage %s' % json_format_date(self.config['startdate'])

    @property
    def total_row(self):
        return []

    def get_value(self, cell):
        if cell:
            return cell['html']
        return 0

    @property
    def rows(self):
        rows_v1 = RecapPassageV1DataSource(config=self.config).get_data()
        rows_v2 = RecapPassageV2DataSource(config=self.config).get_data()

        rows = []
        data = {}
        for row in rows_v1:
            if not data.get(row['product_name']):
                data[row['product_name']] = defaultdict(int)
            product_data = data[row['product_name']]
            product_data['old_stock_total'] += self.get_value(row['old_stock_total'])
            product_data['total_stock'] += self.get_value(row['total_stock'])
            product_data['livraison'] += self.get_value(row['quantity'])
            product_data['stock_total'] += self.get_value(row['stock_total'])
            product_data['old_stock_pps'] += self.get_value(row['old_stock_pps'])
            product_data['outside_receipts_amt'] += self.get_value(row['outside_receipts_amt'])
            product_data['billed_consumption'] += self.get_value(row['billed_consumption'])
            product_data['actual_consumption'] += self.get_value(row['actual_consumption'])
            product_data['pps_stock'] += self.get_value(row['pps_stock'])
            product_data['loss_amt'] += self.get_value(row['loss_amt'])
        for row in rows_v2:
            if not data.get(row['product_name']):
                data[row['product_name']] = defaultdict(int)
            product_data = data[row['product_name']]
            product_data['old_stock_total'] += self.get_value(row['old_stock_total'])
            product_data['total_stock'] += self.get_value(row['total_stock'])
            product_data['livraison'] += self.get_value(row['stock_total']) - self.get_value(row['total_stock'])
            product_data['stock_total'] += self.get_value(row['stock_total'])
            product_data['old_stock_pps'] += self.get_value(row['old_stock_pps'])
            product_data['outside_receipts_amt'] += self.get_value(row['outside_receipts_amt'])
            product_data['billed_consumption'] += self.get_value(row['billed_consumption'])
            product_data['actual_consumption'] += self.get_value(row['actual_consumption'])
            product_data['pps_stock'] += self.get_value(row['pps_stock'])
            product_data['loss_amt'] += self.get_value(row['loss_amt'])

        for product_name, product_data in data.items():
            rows.append([
                product_name,
                product_data['old_stock_total'],
                product_data['total_stock'],
                product_data['livraison'],
                product_data['stock_total'],
                product_data['old_stock_pps'],
                product_data['outside_receipts_amt'],
                (product_data['actual_consumption'] or 0) - (product_data['billed_consumption'] or 0),
                product_data['billed_consumption'],
                product_data['actual_consumption'],
                product_data['stock_total'],
                product_data['pps_stock'],
                product_data['loss_amt'],
            ])
        return rows

    @property
    def headers(self):
        return DataTablesHeader(
            DataTablesColumn('Designations'),
            DataTablesColumn('Stock apres derniere livraison'),
            DataTablesColumn('Stock disponible et utilisable a la livraison'),
            DataTablesColumn('Livraison'),
            DataTablesColumn('Stock Total'),
            DataTablesColumn('Precedent'),
            DataTablesColumn('Recu hors entrepots mobiles'),
            DataTablesColumn('Non Facturable'),
            DataTablesColumn('Facturable'),
            DataTablesColumn('Reelle'),
            DataTablesColumn('Stock Total'),
            DataTablesColumn('PPS Restant'),
            DataTablesColumn('Pertes et Adjustement'),
        )


class ConsommationDataSourceMixin(IntraHealthSqlData):
    slug = 'consommation_data_source'
    show_total = False
    title = 'Consommation Data Source'

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = ['product_name']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')

        return group_by

    @property
    def columns(self):
        columns = []
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))

        columns.append(DatabaseColumn(_("Consumption"), SumColumn('actual_consumption')))
        return columns


class ConsommationV1DataSource(ConsommationDataSourceMixin):
    slug = 'ConsommationV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class ConsommationV2DataSource(ConsommationDataSourceMixin):
    slug = 'ConsommationV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class ConsommationData2(IntraHealthSqlData):
    slug = 'consommation2'
    title = 'Consommation'
    show_total = False

    @property
    def total_row(self):
        return []

    def get_value(self, cell):
        if cell:
            return cell['html']
        return 0

    @property
    def rows(self):
        rows_v1 = ConsommationV1DataSource(config=self.config).get_data()
        rows_v2 = ConsommationV2DataSource(config=self.config).get_data()
        if 'region_id' in self.config:
            loc_name = 'district_name'
        else:
            loc_name = 'PPS_name'

        rows = []
        data = defaultdict(int)
        for row in rows_v1:
            if row[loc_name]:
                data[row[loc_name]] += self.get_value(row['actual_consumption'])
        for row in rows_v2:
            if row[loc_name]:
                data[row[loc_name]] += self.get_value(row['actual_consumption'])
        for loc_name, value in data.items():
            rows.append([loc_name, value])
        return rows

    @property
    def headers(self):
        if 'region_id' in self.config:
            headers = DataTablesHeader(
                DataTablesColumn('District'),
            )
        else:
            headers = DataTablesHeader(
                DataTablesColumn('PPS'),
            )
        headers.add_column(DataTablesColumn('Consumption'))
        return headers


class PPSAvecDonneesDataSourceMixin(IntraHealthSqlData):
    slug = 'pps_avec_donnees_data_source'
    show_total = False
    title = 'PPS Avec Données Data Source'

    @property
    def filters(self):
        filters = [BETWEEN("real_date_repeat", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = ['pps_id']
        if 'region_id' in self.config:
            group_by.append('district_name')
        else:
            group_by.append('PPS_name')
        return group_by

    @property
    def columns(self):
        columns = [DatabaseColumn(_("PPS ID"), SimpleColumn('pps_id'))]
        if 'region_id' in self.config:
            columns.append(DatabaseColumn(_("District"), SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn(_("PPS"), SimpleColumn('PPS_name')))
        return columns

    @property
    def values_per_loc(self):
        values = {}
        if 'region_id' in self.config:
            loc_name = 'district_name'
        else:
            loc_name = 'PPS_name'
        rows = self.get_data()
        for row in rows:
            if row[loc_name]:
                if row[loc_name] not in values:
                    values[row[loc_name]] = set()
                values[row[loc_name]].add(row['pps_id'])
        return values


class PPSAvecDonneesV1DataSource(PPSAvecDonneesDataSourceMixin):
    slug = 'PPSAvecDonneesV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class PPSAvecDonneesV2DataSource(PPSAvecDonneesDataSourceMixin):
    slug = 'PPSAvecDonneesV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class PPSAvecDonnees2(IntraHealthSqlData):
    slug = 'pps_avec_donnees2'
    title = 'PPS Avec Données'
    show_total = False

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        values_v1 = PPSAvecDonneesV1DataSource(config=self.config).values_per_loc
        values_v2 = PPSAvecDonneesV2DataSource(config=self.config).values_per_loc
        values = {}
        for loc_name, pps_ids in values_v1.items():
            if loc_name not in values:
                values[loc_name] = set()
            for pps_id in pps_ids:
                values[loc_name].add(pps_id)
        for loc_name, pps_ids in values_v2.items():
            if loc_name not in values:
                values[loc_name] = set()
            for pps_id in pps_ids:
                values[loc_name].add(pps_id)

        rows = []
        for loc_name in values:
            rows.append([loc_name, 1])
        if 'district_id' in self.config:
            locations_included = [loc_name for loc_name in values]
        else:
            return rows

        all_locations = SQLLocation.objects.get(
            location_id=self.config['district_id']
        ).get_children().exclude(is_archived=True).values_list('name', flat=True)
        locations_not_included = set(all_locations) - set(locations_included)
        return rows + [[location, {'sort_key': 0, 'html': 0}] for location in locations_not_included]

    @property
    def headers(self):
        if 'district_id' in self.config:
            headers = DataTablesHeader(
                DataTablesColumn('District'),
            )
        else:
            headers = DataTablesHeader(
                DataTablesColumn('PPS'),
            )
        headers.add_column(DataTablesColumn('PPS Avec Données Soumises'))
        return headers


class ConventureDataSourceMixin(IntraHealthSqlData):
    slug = 'conventureDataSourceMixin'
    title = 'Converture'
    show_total = False
    custom_total_calculate = True

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        group_by = ['pps_id', 'real_date']

        if 'district_id' in self.config:
            group_by.append('district_id')
        else:
            group_by.append('region_id')

        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn('Mois', MonthColumn('real_date')),
            DatabaseColumn('pps_id', SimpleColumn('pps_id')),
        ]
        if 'district_id' in self.config:
            columns.append(DatabaseColumn('district_id', SimpleColumn('district_id')))
        else:
            columns.append(DatabaseColumn('region_id', SimpleColumn('region_id')))
        return columns

    @property
    def values_per_month(self):
        values = {}
        if 'district_id' in self.config:
            loc_name = 'district_id'
        else:
            loc_name = 'region_id'
        registered = 0
        rows = self.get_data()
        for row in rows:
            month = int(row['real_date']['html'])
            if month not in values:
                values[month] = {
                    'pps_ids': set(),
                }
            if row.get(loc_name):
                loc = get_location(row[loc_name], domain=self.config['domain'])
                new_registered = _locations_per_type(self.config['domain'], 'PPS', loc)
                if new_registered > registered:
                    registered = new_registered
            values[month]['pps_ids'].add(row['pps_id'])

        return values, registered


class ConventureV1DataSource(ConventureDataSourceMixin):
    slug = 'conventureV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class ConventureV2DataSource(ConventureDataSourceMixin):
    slug = 'conventureV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class ConventureData2(IntraHealthSqlData):
    slug = 'conventure2'
    title = 'Converture'
    show_total = False

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        values_v1, registered_v1 = \
            ConventureV1DataSource(config=self.config).values_per_month
        values_v2, registered_v2 = \
            ConventureV2DataSource(config=self.config).values_per_month

        registered = registered_v1 if registered_v1 > registered_v2 else registered_v2

        rows = []
        months = set()
        for month in values_v1:
            months.add(month)
        for month in values_v2:
            months.add(month)
        months = sorted(months)
        for month in months:
            from custom.intrahealth.reports.utils import get_localized_months
            pps_ids = set()
            if values_v1.get(month):
                pps_ids = values_v1[month]['pps_ids']
            if values_v2.get(month):
                pps_ids = pps_ids.union(values_v2[month]['pps_ids'])
            unique = len(pps_ids)
            rows.append([get_localized_months()[month], registered, 0, unique, self.percent_fn(registered, unique),
                         unique, self.percent_fn(unique, unique)])
        return rows

    @property
    def headers(self):
        return DataTablesHeader(
            DataTablesColumn('Mois'),
            DataTablesColumn('No de PPS (number of PPS registered in that region)'),
            DataTablesColumn('No de PPS planifie (number of PPS planned)'),
            DataTablesColumn('No de PPS avec livrasion cet mois (number of PPS visited this month)'),
            DataTablesColumn('Taux de couverture (coverage ratio)'),
            DataTablesColumn('No de PPS avec donnees soumises (number of PPS which submitted data)'),
            DataTablesColumn('Exhaustivite des donnees'),
        )


class FicheDataSourceMixin(IntraHealthSqlData):
    slug = 'ficheDataSourceMixin'
    title = ''
    show_total = True

    @property
    def filters(self):
        filters = [BETWEEN("real_date_repeat", "startdate", "enddate")]
        if 'region_id' in self.config:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config:
            filters.append(EQ("district_id", "district_id"))
        if 'archived_locations' in self.config:
            filters.append(_locations_filter(self.config['archived_locations']))
        return filters

    @property
    def group_by(self):
        return ['product_name', 'PPS_name']

    @property
    def values_for_pps(self):
        values = {}

        rows = self.get_data()
        for row in rows:
            PPS_name = row['PPS_name']
            if PPS_name not in values:
                values[PPS_name] = {}
            productName = row['product_name']
            values[PPS_name][productName] = [
                row['actual_consumption']['html'],
                row['billed_consumption']['html']
            ]

        return values

    @property
    def columns(self):
        return [
            DatabaseColumn('Product Name', SimpleColumn('product_name')),
            DatabaseColumn('LISTE des PPS', SimpleColumn('PPS_name')),
            DatabaseColumn("Consommation Reelle", SumColumn('actual_consumption')),
            DatabaseColumn("Consommation Facturable", SumColumn('billed_consumption'))
        ]


class FicheV1DataSource(FicheDataSourceMixin):
    slug = 'ficheV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V1)


class FicheV2DataSource(FicheDataSourceMixin):
    slug = 'ficheV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], OPERATEUR_V2)


class FicheData2(IntraHealthSqlData):
    slug = 'fiche_data'
    title = ''
    show_total = False
    product_names = set()
    pps_names = set()

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        values_v1 = FicheV1DataSource(config=self.config).values_for_pps
        values_v2 = FicheV2DataSource(config=self.config).values_for_pps

        for pps_name, values in values_v1.items():
            self.pps_names.add(pps_name)
            for product_name in values:
                self.product_names.add(product_name)
        for pps_name, values in values_v2.items():
            self.pps_names.add(pps_name)
            for product_name in values:
                self.product_names.add(product_name)
        self.pps_names = sorted(self.pps_names)
        self.product_names = sorted(self.product_names)
        rows = []
        for pps_name in self.pps_names:
            row = [pps_name]
            for product_name in self.product_names:
                values_for_product_v1 = values_v1[pps_name][product_name] if \
                    pps_name in values_v1 and product_name in values_v1[pps_name] else [0, 0]
                values_for_product_v2 = values_v2[pps_name][product_name] if \
                    pps_name in values_v2 and product_name in values_v2[pps_name] else [0, 0]
                actual_consumption = values_for_product_v1[0] + values_for_product_v2[0]
                billed_consumption = values_for_product_v1[1] + values_for_product_v2[1]
                row.append(actual_consumption)
                row.append(billed_consumption)
                row.append((actual_consumption or 0) - (billed_consumption or 0))
            rows.append(row)
        return rows

    @property
    def headers(self):
        headers = DataTablesHeader()
        headers.add_column(DataTablesColumnGroup('', DataTablesColumn('LISTE des PPS')))

        for product_name in self.product_names:
            headers.add_column(DataTablesColumnGroup(
                product_name,
                DataTablesColumn('Consommation Reelle'),
                DataTablesColumn('Consommation Facturable'),
                DataTablesColumn('Consommation Non Facturable')
            ))

        return headers


class CommandeDataSourceMixin(IntraHealthSqlData):
    slug = 'commandeDataSourceMixin'
    title = 'Taux de satisfaction de la commande de l\'operateur'
    show_total = False

    @property
    def group_by(self):
        return ['productName']

    @property
    @memoized
    def products(self):
        return list(SQLProduct.objects.filter(domain=self.config['domain'], is_archived=False).order_by('name'))

    @property
    def values_for_product(self):
        products = self.products
        values = {
            product.name: [0, 0]
            for product in products
        }

        rows = self.get_data()
        for row in rows:
            productName = row['productName']
            values[productName] = [
                row['amountOrdered']['html'],
                row['amountReceived']['html']
            ]

        return values

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('Quantity'))
        for product in self.products:
            headers.add_column(DataTablesColumn(product.name))
        return headers

    @property
    def columns(self):
        return [
            DatabaseColumn('Product Name', SimpleColumn('productName')),
            DatabaseColumn("Commandes", SumColumn('amountOrdered')),
            DatabaseColumn("Recu", SumColumn('amountReceived'))
        ]


class CommandeV1DataSource(CommandeDataSourceMixin):
    slug = 'commandeV1DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], COMMANDE_V1)


class CommandeV2DataSource(CommandeDataSourceMixin):
    slug = 'commandeV2DataSource'

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], COMMANDE_V2)


class DispDesProducts2(IntraHealthSqlData):
    slug = 'products'
    title = 'Taux de satisfaction de la commande de l\'operateur'
    show_total = False
    product_names = set()

    @property
    def total_row(self):
        return []

    @property
    def rows(self):
        values_v1 = CommandeV1DataSource(config=self.config).values_for_product
        values_v2 = CommandeV2DataSource(config=self.config).values_for_product

        commandes = ['Commandes']
        raux = ['Raux']
        taux = ['Taux']

        for product_name in values_v1:
            self.product_names.add(product_name)
        for product_name in values_v2:
            self.product_names.add(product_name)
        self.product_names = sorted(self.product_names)
        for product_name in self.product_names:
            values_for_product_v1 = values_v1[product_name] if product_name in values_v1 else [0, 0]
            values_for_product_v2 = values_v2[product_name] if product_name in values_v2 else [0, 0]
            amountOrdered = values_for_product_v1[0] + values_for_product_v2[0]
            amountReceived = values_for_product_v1[1] + values_for_product_v2[1]
            commandes.append(amountOrdered)
            raux.append(amountReceived)
            taux.append("%d%%" % (100 * amountOrdered / (amountReceived or 1)))
        return [commandes, raux, taux]

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('Quantity'))
        for product_name in self.product_names:
            headers.add_column(DataTablesColumn(product_name))
        return headers


class ContainsFilter(SqlFilter):
    def __init__(self, column_name, contains):
        self.column_name = column_name
        self.contains = contains

    def build_expression(self, table):
        column = get_column(table, self.column_name)
        return column.like("%{0}%".format(self.contains))


class CustomEQFilter(SqlFilter):
    """
    EQ Filter without binding parameter
    """

    def __init__(self, column_name, parameter):
        self.column_name = column_name
        self.parameter = parameter

    def build_expression(self, table):
        column = get_column(table, self.column_name)
        return column.match(self.parameter)


class YeksiSqlData(SqlData):
    datatables = False
    show_charts = False
    show_total = True
    custom_total_calculate = False
    no_value = {'sort_key': 0, 'html': 0}
    fix_left_col = False

    @property
    def engine_id(self):
        return 'ucr'

    def percent_fn(self, x, y, nominal_denominator=1):
        return "{:.2f}%".format(100 * float(x or 0) / float(y or nominal_denominator))

    @property
    @memoized
    def months(self):
        return [month.date() for month in
                rrule(freq=MONTHLY, dtstart=self.config['startdate'], until=self.config['enddate'])]

    def date_in_selected_date_range(self, date):
        return self.months[0] <= date < self.months[-1] + relativedelta(months=1)

    def denominator_exists(self, denominator):
        return denominator and denominator['html']

    def get_index_of_month_in_selected_data_range(self, date):
        for index in range(len(self.months)):
            if date < self.months[index] + relativedelta(months=1):
                return index

    def cell_value_less_than(self, cell, value):
        return True if cell != 'pas de données' and float(cell[:-1]) < value else False

    def cell_value_bigger_than(self, cell, value):
        return True if cell != 'pas de données' and float(cell[:-1]) > value else False

    def month_headers(self):
        month_headers = []
        french_months = {
            1: 'Janvier',
            2: 'Février',
            3: 'Mars',
            4: 'Avril',
            5: 'Mai',
            6: 'Juin',
            7: 'Juillet',
            8: 'Août',
            9: 'Septembre',
            10: 'Octobre',
            11: 'Novembre',
            12: 'Décembre',
        }
        for month in self.months:
            month_headers.append(DataTablesColumn("{0} {1}".format(french_months[month.month], month.year)))
        return month_headers


class VisiteDeLOperateurDataSource(YeksiSqlData):

    @property
    def filters(self):
        filters = [BETWEEN("real_date", "startdate", "enddate")]
        program_id = self.config.get('program')
        if program_id:
            filters.append(ContainsFilter("select_programs", program_id))
        if 'region_id' in self.config and self.config['region_id']:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config and self.config['district_id']:
            filters.append(EQ("district_id", "district_id"))
        elif 'pps_id' in self.config and self.config['pps_id']:
            filters.append(EQ("pps_id", "pps_id"))
        return filters

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR)

    @cached_property
    def loc_id(self):
        if 'pps_id' in self.config or 'district_id' in self.config:
            return 'pps_id'
        elif 'region_id' in self.config:
            return 'district_id'
        else:
            return 'region_id'

    @cached_property
    def loc_name(self):
        if 'pps_id' in self.config or 'district_id' in self.config:
            return 'pps_name'
        elif 'region_id' in self.config:
            return 'district_name'
        else:
            return 'region_name'

    @property
    def headers(self):
        if self.loc_id == 'pps_id':
            first_row = 'PPS'
        elif self.loc_id == 'district_id':
            first_row = 'District'
        else:
            first_row = 'Région'

        headers = DataTablesHeader(DataTablesColumn(first_row))
        for month in self.month_headers():
            headers.add_column(month)
        return headers


class VisiteDeLOperateurPerProductDataSource(YeksiSqlData):

    @property
    def filters(self):
        filters = [BETWEEN("real_date_repeat", "startdate", "enddate")]
        program_id = self.config.get('program')
        if program_id:
            programs = ProductsInProgramData(config={'domain': self.config['domain']}).rows
            for program in programs:
                if program_id == program[0]:
                    filters.append(OR(
                        [CustomEQFilter("product_id", product) for product in program[1].split(" ")]
                    ))
                    break
        if 'region_id' in self.config and self.config['region_id']:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config and self.config['district_id']:
            filters.append(EQ("district_id", "district_id"))
        elif 'pps_id' in self.config and self.config['pps_id']:
            filters.append(EQ("pps_id", "pps_id"))
        return filters

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR_PER_PRODUCT)

    @cached_property
    def loc_id(self):
        if 'pps_id' in self.config or 'district_id' in self.config:
            return 'pps_id'
        elif 'region_id' in self.config:
            return 'district_id'
        else:
            return 'region_id'

    @cached_property
    def loc_name(self):
        if 'pps_id' in self.config or 'district_id' in self.config:
            return 'pps_name'
        elif 'region_id' in self.config:
            return 'district_name'
        else:
            return 'region_name'

    @property
    def headers(self):
        if self.loc_id == 'pps_id':
            first_row = 'PPS'
        elif self.loc_id == 'district_id':
            first_row = 'District'
        else:
            first_row = 'Région'

        headers = DataTablesHeader(DataTablesColumn(first_row))
        for month in self.month_headers():
            headers.add_column(month)
        return headers


class LogisticienDataSource(YeksiSqlData):

    @property
    def filters(self):
        filters = [BETWEEN("date_echeance", "startdate", "enddate")]
        if 'region_id' in self.config and self.config['region_id']:
            filters.append(EQ("region_id", "region_id"))
        elif 'district_id' in self.config and self.config['district_id']:
            filters.append(EQ("district_id", "district_id"))
        return filters

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], YEKSI_NAA_REPORTS_LOGISTICIEN)

    @cached_property
    def loc_id(self):
        if 'region_id' in self.config:
            return 'district_id'
        else:
            return 'region_id'

    @cached_property
    def loc_name(self):
        if 'region_id' in self.config:
            return 'district_name'
        else:
            return 'region_name'

    @property
    def headers(self):
        if self.loc_id == 'district_id':
            first_row = 'District'
        else:
            first_row = 'Région'

        headers = DataTablesHeader(DataTablesColumn(first_row))
        for month in self.month_headers():
            headers.add_column(month)
        return headers


class ProgramsDataSource(YeksiSqlData):

    @property
    def filters(self):
        return []

    @property
    def table_name(self):
        return get_table_name(self.config['domain'], YEKSI_NAA_REPORTS_VISITE_DE_L_OPERATOUR_PER_PROGRAM)

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('ID'))
        headers.add_column(DataTablesColumn('Name'))
        return headers


class ProgramData(ProgramsDataSource):
    slug = 'program'
    comment = 'Program names'
    title = 'Program'
    show_total = False

    @property
    def group_by(self):
        group_by = ['program_id', 'program_name']
        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Program ID", SimpleColumn('program_id')),
            DatabaseColumn("Program Name", SimpleColumn('program_name')),
        ]
        return columns

    @property
    def rows(self):
        records = self.get_data()
        rows = []
        for record in records:
            rows.append([record['program_id'], record['program_name']])
        return sorted(rows, key=lambda x: x[0])


class ProductsInProgramData(ProgramsDataSource):
    """
    Returns list of all product ids used in program as string joined by spaces
    """
    slug = 'products_in_program'
    comment = 'Products selected per program'
    title = 'Products selected per program'
    show_total = False

    @property
    def group_by(self):
        group_by = ['program_id', 'product_ids']
        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Program ID", SimpleColumn('program_id')),
            DatabaseColumn("Product IDs", SimpleColumn('product_ids')),
        ]
        return columns

    @property
    def rows(self):
        records = self.get_data()
        programs = defaultdict(set)
        for record in records:
            products = record['product_ids'].split(' ')
            for product in products:
                programs[record['program_id']].add(product)

        rows = []
        for program_id, products in programs.items():
            rows.append([program_id, " ".join(products)])
        return sorted(rows, key=lambda x: x[0])


class AvailabilityData(VisiteDeLOperateurDataSource):
    slug = 'disponibilite'
    comment = 'Disponibilité de la gamme au niveau PPS : combien de PPS ont eu tous les produits disponibles'
    title = 'Disponibilité'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, rows):
        total_row = [{
            'html': 'Disponibilité (%)',
        }]
        total_numerator = 0
        total_denominator = 0
        if self.loc_id == 'pps_id':
            data = {}
            for i in range(len(self.months)):
                data[i] = {
                    'pps_is_available': sum(
                        1 for pps_data in rows if pps_data[i + 1]['html'] == '100%'
                    ),
                    'pps_count': sum(1 for pps_data in rows
                                     if pps_data[i + 1]['html'] != 'pas de données')
                }
                if data[i]['pps_count']:
                    month_value = self.percent_fn(
                        data[i]['pps_is_available'],
                        data[i]['pps_count']
                    )
                    total_row.append({
                        'html': month_value,
                        'style': 'color: red' if self.cell_value_less_than(month_value, 95) else '',
                    })
                    total_numerator += data[i]['pps_is_available']
                    total_denominator += data[i]['pps_count']
                else:
                    total_row.append({
                        'html': 'pas de données',
                    })
            if total_denominator:
                total_value = self.percent_fn(
                    total_numerator,
                    total_denominator
                )
                total_row.append({
                    'html': total_value,
                    'style': 'color: red' if self.cell_value_less_than(total_value, 95) else '',
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        else:
            for i in range(len(self.months)):
                numerator = 0
                denominator = 0
                for location in rows:
                    numerator += sum(rows[location][i].values())
                    denominator += len(rows[location][i])
                total_numerator += numerator
                total_denominator += denominator
                if denominator:
                    month_value = self.percent_fn(numerator, denominator)
                    total_row.append({
                        'html': month_value,
                        'style': 'color: red' if self.cell_value_less_than(month_value, 95) else '',
                    })
                else:
                    total_row.append({
                        'html': 'pas de données',
                    })
            if total_denominator:
                total_value = self.percent_fn(total_numerator, total_denominator)
                total_row.append({
                    'html': total_value,
                    'style': 'color: red' if self.cell_value_less_than(total_value, 95) else '',
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        return total_row

    @property
    def group_by(self):
        group_by = ['real_date', 'pps_id', self.loc_name]
        if self.loc_id != 'pps_id':
            group_by.append(self.loc_id)
        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn("PPS ID", SimpleColumn('pps_id')),
            DatabaseColumn("Date", SimpleColumn('real_date')),
            DatabaseColumn("Number of PPS with stockout", MaxColumn('pps_is_outstock')),
        ]
        if self.loc_id == 'pps_id':
            columns.append(DatabaseColumn("PPS Name", SimpleColumn('pps_name')))
        elif self.loc_id == 'district_id':
            columns.append(DatabaseColumn("District ID", SimpleColumn('district_id')))
            columns.append(DatabaseColumn("District Name", SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn("Region ID", SimpleColumn('region_id')))
            columns.append(DatabaseColumn("Region Name", SimpleColumn('region_name')))
        return columns

    def get_availability_data_per_month_per_pps(self, records):
        data = {}
        loc_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date']):
                continue
            if record[self.loc_id] not in data:
                data[record[self.loc_id]] = ['pas de données'] * len(self.months)
                loc_names[record[self.loc_id]] = record[self.loc_name]
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date'])
            data[record[self.loc_id]][month_index] = '0%' if record['pps_is_outstock']['html'] == 1 else '100%'
        return loc_names, data

    def get_availability_data_per_month_aggregated(self, records):
        data = defaultdict(list)
        loc_names = {}
        new_data = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date']):
                continue
            if record[self.loc_id] not in data:
                for i in range(len(self.months)):
                    data[record[self.loc_id]].append(defaultdict(int))
                loc_names[record[self.loc_id]] = record[self.loc_name]
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date'])
            no_multiple_rows_per_pps_in_month = \
                data[record[self.loc_id]][month_index].get(record['pps_id']) is None
            if no_multiple_rows_per_pps_in_month or \
                    data[record[self.loc_id]][month_index][record['pps_id']] == 1:
                data[record[self.loc_id]][month_index][record['pps_id']] = 0 if \
                    record['pps_is_outstock']['html'] == 1 else 1

        for location in data:
            new_data[location] = ['pas de données'] * len(self.months)
            for month_index in range(len(self.months)):
                if data[location][month_index]:
                    new_data[location][month_index] = self.percent_fn(
                        sum(data[location][month_index].values()),
                        len(data[location][month_index])
                    )
        return loc_names, new_data, data

    def get_average_availability_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month != 'pas de données':
                if self.loc_id == 'pps_id':
                    if data_in_month == '100%':
                        numerator += 1
                else:
                    numerator += float(data_in_month[:-1])
                denominator += 1
        if denominator:
            if self.loc_id == 'pps_id':
                return "{:.2f}%".format(numerator * 100 / denominator)
            else:
                return "{:.2f}%".format(numerator / denominator)
        else:
            return 'pas de données'

    def parse_availability_data_to_rows(self, loc_names, data):
        rows = []
        for loc_id in data:
            row = [{
                'html': loc_names[loc_id],
            }]
            for cell in data[loc_id]:
                row.append({
                    'html': cell,
                    'style': 'color: red' if self.cell_value_less_than(cell, 95) else '',
                })
            average = self.get_average_availability_in_location(data[loc_id])
            row.append({
                'html': average,
                'style': 'color: red' if self.cell_value_less_than(average, 95) else '',
            })
            rows.append(row)
        return rows

    @property
    def rows(self):
        records = self.get_data()

        if self.loc_id == 'pps_id':
            loc_names, data = self.get_availability_data_per_month_per_pps(records)
        else:
            loc_names, data, tmp = self.get_availability_data_per_month_aggregated(records)
            self.total_row = self.calculate_total_row(tmp)

        rows = self.parse_availability_data_to_rows(loc_names, data)
        if self.loc_id == 'pps_id':
            self.total_row = self.calculate_total_row(rows)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = super(AvailabilityData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen de disponibilité'))
        return headers


class LossRateData(VisiteDeLOperateurPerProductDataSource):
    slug = 'taux_de_perte'
    comment = 'Taux de Perte (hors péremption)'
    title = 'Taux de Perte (hors péremption)'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, data):
        if 'region_id' in self.config and self.config['region_id']:
            total_row = [{
                'html': 'Taux par Région',
            }]
        elif 'district_id' in self.config and self.config['district_id']:
            total_row = [{
                'html': 'Taux par District',
            }]
        elif 'pps_id' in self.config and self.config['pps_id']:
            total_row = [{
                'html': '',
            }]
        else:
            total_row = [{
                'html': 'Taux par Pays',
            }]
        total_numerator = 0
        total_denominator = 0
        for i in range(len(self.months)):
            numerator = sum(
                data[loc_id][i]['loss_amt'] for loc_id in data if
                data[loc_id][i]['final_pna_stock']
            )
            denominator = sum(
                data[loc_id][i]['final_pna_stock'] for loc_id in data if
                data[loc_id][i]['final_pna_stock']
            )
            total_numerator += numerator
            total_denominator += denominator
            total_value = self.percent_fn(
                numerator,
                denominator
            )
            if denominator:
                total_row.append({
                    'html': total_value,
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        total_value = self.percent_fn(
            total_numerator,
            total_denominator
        )
        if total_denominator:
            total_row.append({
                'html': total_value,
            })
        else:
            total_row.append({
                'html': 'pas de données',
            })
        return total_row

    @property
    def group_by(self):
        return ['real_date_repeat', self.loc_id, self.loc_name]

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Date", SimpleColumn('real_date_repeat')),
            DatabaseColumn("Total number of PNA lost product", SumColumn('loss_amt')),
            DatabaseColumn("PNA final stock", SumColumn('final_pna_stock')),
        ]
        if self.loc_id == 'pps_id':
            columns.append(DatabaseColumn("PPS ID", SimpleColumn('pps_id')))
            columns.append(DatabaseColumn("PPS Name", SimpleColumn('pps_name')))
        elif self.loc_id == 'district_id':
            columns.append(DatabaseColumn("District ID", SimpleColumn('district_id')))
            columns.append(DatabaseColumn("District Name", SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn("Region ID", SimpleColumn('region_id')))
            columns.append(DatabaseColumn("Region Name", SimpleColumn('region_name')))
        return columns

    def get_average_loss_rate_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month['final_pna_stock']:
                numerator += data_in_month['loss_amt']
                denominator += data_in_month['final_pna_stock']
        if denominator:
            value = self.percent_fn(
                numerator,
                denominator,
            )
            return {
                'html': value,
            }
        else:
            return {
                'html': 'pas de données',
            }

    def parse_loss_rate_to_rows(self, loc_names, data):
        rows = []
        for loc_id in data:
            row = [{
                'html': loc_names[loc_id],
            }]
            for i in range(len(self.months)):
                if data[loc_id][i]['final_pna_stock']:
                    month_value = self.percent_fn(
                        data[loc_id][i]['loss_amt'],
                        data[loc_id][i]['final_pna_stock']
                    )
                    row.append({
                        'html': month_value,
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                    })
            row.append(self.get_average_loss_rate_in_location(data[loc_id]))
            rows.append(row)
        return rows

    def get_loss_rate_per_month(self, records):
        data = defaultdict(list)
        loc_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_repeat']):
                continue
            if record[self.loc_id] not in data:
                for i in range(len(self.months)):
                    data[record[self.loc_id]].append(defaultdict(int))
                loc_names[record[self.loc_id]] = record[self.loc_name]
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date_repeat'])
            if self.denominator_exists(record['final_pna_stock']):
                if record['loss_amt']:
                    data[record[self.loc_id]][month_index]['loss_amt'] += record['loss_amt']['html']
                data[record[self.loc_id]][month_index]['final_pna_stock'] += record['final_pna_stock']['html']
        return loc_names, data

    @property
    def rows(self):
        records = self.get_data()
        loc_names, data = self.get_loss_rate_per_month(records)
        self.total_row = self.calculate_total_row(data)
        rows = self.parse_loss_rate_to_rows(loc_names, data)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = super(LossRateData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen'))
        return headers


class ExpirationRateData(VisiteDeLOperateurPerProductDataSource):
    slug = 'taux_de_peremption'
    comment = 'valeur péremption sur valeur totale'
    title = 'Taux de Péremption'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, data):
        if 'region_id' in self.config and self.config['region_id']:
            total_row = [{
                'html': 'Taux par Région',
            }]
        elif 'district_id' in self.config and self.config['district_id']:
            total_row = [{
                'html': 'Taux par District',
            }]
        elif 'pps_id' in self.config and self.config['pps_id']:
            total_row = [{
                'html': '',
            }]
        else:
            total_row = [{
                'html': 'Taux par Pays',
            }]
        total_numerator = 0
        total_denominator = 0
        for i in range(len(self.months)):
            numerator = sum(
                data[loc_id][i]['expired_pna_valuation'] for loc_id in data if
                data[loc_id][i]['final_pna_stock_valuation']
            )
            denominator = sum(
                data[loc_id][i]['final_pna_stock_valuation'] for loc_id in data if
                data[loc_id][i]['final_pna_stock_valuation']
            )
            total_numerator += numerator
            total_denominator += denominator
            total_value = self.percent_fn(
                numerator,
                denominator
            )
            if denominator:
                total_row.append({
                    'html': total_value,
                    'style': 'color: red' if self.cell_value_bigger_than(total_value, 5) else '',
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        total_value = self.percent_fn(
            total_numerator,
            total_denominator
        )
        if total_denominator:
            total_row.append({
                'html': total_value,
                'style': 'color: red' if self.cell_value_bigger_than(total_value, 5) else '',
            })
        else:
            total_row.append({
                'html': 'pas de données',
            })
        return total_row

    @property
    def group_by(self):
        return ['real_date_repeat', self.loc_id, self.loc_name]

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Date", SimpleColumn('real_date_repeat')),
            DatabaseColumn("Expired products valuation", SumColumn('expired_pna_valuation')),
            DatabaseColumn("Products stock valuation", SumColumn('final_pna_stock_valuation')),
        ]
        if self.loc_id == 'pps_id':
            columns.append(DatabaseColumn("PPS ID", SimpleColumn('pps_id')))
            columns.append(DatabaseColumn("PPS Name", SimpleColumn('pps_name')))
        elif self.loc_id == 'district_id':
            columns.append(DatabaseColumn("District ID", SimpleColumn('district_id')))
            columns.append(DatabaseColumn("District Name", SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn("Region ID", SimpleColumn('region_id')))
            columns.append(DatabaseColumn("Region Name", SimpleColumn('region_name')))
        return columns

    def get_average_expiration_rate_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month['final_pna_stock_valuation']:
                numerator += data_in_month['expired_pna_valuation']
                denominator += data_in_month['final_pna_stock_valuation']
        if denominator:
            value = self.percent_fn(
                numerator,
                denominator,
            )
            return {
                'html': value,
                'style': 'color: red' if self.cell_value_bigger_than(value, 5) else '',
            }
        else:
            return {
                'html': 'pas de données',
            }

    def parse_expiration_rate_to_rows(self, loc_names, data):
        rows = []
        for loc_id in data:
            row = [{
                'html': loc_names[loc_id],
            }]
            for i in range(len(self.months)):
                if data[loc_id][i]['final_pna_stock_valuation']:
                    month_value = self.percent_fn(
                        data[loc_id][i]['expired_pna_valuation'],
                        data[loc_id][i]['final_pna_stock_valuation']
                    )
                    row.append({
                        'html': month_value,
                        'style': 'color: red' if self.cell_value_bigger_than(month_value, 5) else '',
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                    })
            row.append(self.get_average_expiration_rate_in_location(data[loc_id]))
            rows.append(row)
        return rows

    def get_expiration_rate_per_month(self, records):
        data = defaultdict(list)
        loc_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_repeat']):
                continue
            if record[self.loc_id] not in data:
                for i in range(len(self.months)):
                    data[record[self.loc_id]].append(defaultdict(int))
                loc_names[record[self.loc_id]] = record[self.loc_name]
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date_repeat'])
            if self.denominator_exists(record['final_pna_stock_valuation']):
                if record['expired_pna_valuation']:
                    data[record[self.loc_id]][month_index]['expired_pna_valuation'] += \
                        record['expired_pna_valuation']['html']
                data[record[self.loc_id]][month_index]['final_pna_stock_valuation'] += \
                    record['final_pna_stock_valuation']['html']
        return loc_names, data

    @property
    def rows(self):
        records = self.get_data()
        loc_names, data = self.get_expiration_rate_per_month(records)
        self.total_row = self.calculate_total_row(data)
        rows = self.parse_expiration_rate_to_rows(loc_names, data)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = super(ExpirationRateData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen'))
        return headers


class RecoveryRateByPPSData(VisiteDeLOperateurDataSource):
    slug = 'taux_de_recouvrement_au_niveau_du_pps'
    comment = 'Somme des montants payés sur total dû'
    title = 'Taux de Recouvrement au niveau du PPS'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, data, value_partials):
        if 'region_id' in self.config and self.config['region_id']:
            total_row = [{
                'html': 'Taux par Région',
            }]
        elif 'district_id' in self.config and self.config['district_id']:
            total_row = [{
                'html': 'Taux par District',
            }]
        elif 'pps_id' in self.config and self.config['pps_id']:
            total_row = [{
                'html': 'Taux par PPS',
            }]
        else:
            total_row = [{
                'html': 'Taux par Pays',
            }]
        for i in range(len(self.months)):
            numerator = sum(
                data[loc_id][i]['pps_total_amt_paid'] for loc_id in data if
                data[loc_id][i]['pps_total_amt_owed']
            )
            denominator = sum(
                data[loc_id][i]['delivery_amt_owed'] + data[loc_id][i]['pps_total_amt_owed'] -
                data[loc_id][i]['delivery_amt_owed_first_visit'] for loc_id in data if
                data[loc_id][i]['pps_total_amt_owed']
            )
            total_value = self.percent_fn(
                numerator,
                denominator,
                nominal_denominator=100,
            )
            if denominator or numerator:
                total_row.append({
                    'html': total_value,
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        total_numerator = 0
        total_denominator = 0
        for loc_id, value in value_partials.items():
            total_numerator += value['numerator']
            total_denominator += value['denominator']
        total_value = self.percent_fn(
            total_numerator,
            total_denominator,
            nominal_denominator=100,
        )
        if total_denominator or total_numerator:
            total_row.append({
                'html': total_value,
            })
        else:
            total_row.append({
                'html': 'pas de données',
            })
        return total_row

    @property
    def group_by(self):
        group_by = [
            'doc_id', 'real_date_precise', 'delivery_amt_owed', 'pps_id', self.loc_name, 'pps_total_amt_paid',
            'pps_total_amt_owed'
        ]
        if self.loc_id != 'pps_id':
            group_by.append(self.loc_id)
        return group_by

    @property
    def columns(self):
        columns = [
            DatabaseColumn("DOC ID", SimpleColumn('doc_id')),
            DatabaseColumn("PPS ID", SimpleColumn('pps_id')),
            DatabaseColumn("Date", SimpleColumn('real_date_precise')),
            DatabaseColumn("Delivery Amount Owed", SimpleColumn('delivery_amt_owed')),
            DatabaseColumn("Total amount paid by PPS", SimpleColumn('pps_total_amt_paid')),
            DatabaseColumn("Total amount owed by PPS", SimpleColumn('pps_total_amt_owed')),
        ]
        if self.loc_id == 'pps_id':
            columns.append(DatabaseColumn("PPS Name", SimpleColumn('pps_name')))
        elif self.loc_id == 'district_id':
            columns.append(DatabaseColumn("District ID", SimpleColumn('district_id')))
            columns.append(DatabaseColumn("District Name", SimpleColumn('district_name')))
        else:
            columns.append(DatabaseColumn("Region ID", SimpleColumn('region_id')))
            columns.append(DatabaseColumn("Region Name", SimpleColumn('region_name')))
        return columns

    def get_recovery_rate_by_pps_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        value_partials = {
            'numerator': 0,
            'denominator': 0,
        }
        first_data = True
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month['delivery_amt_owed']:
                numerator += data_in_month['pps_total_amt_paid']
                denominator += data_in_month['delivery_amt_owed']
                if first_data:
                    first_data = False
                    denominator += data_in_month['pps_total_amt_owed']
                    denominator -= data_in_month['delivery_amt_owed_first_visit']
        if denominator or numerator:
            value = self.percent_fn(
                numerator,
                denominator,
                nominal_denominator=100,
            )
            value_partials['numerator'] = numerator
            value_partials['denominator'] = denominator
            return {'html': value}, value_partials
        else:
            return {'html': 'pas de données'}, value_partials

    def parse_recovery_rate_by_pps_to_rows(self, loc_names, data):
        rows = []
        value_partials = {}
        for loc_id in data:
            row = [{
                'html': loc_names[loc_id],
            }]
            for i in range(len(self.months)):
                denominator = \
                    data[loc_id][i]['delivery_amt_owed'] + \
                    data[loc_id][i]['pps_total_amt_owed'] - \
                    data[loc_id][i]['delivery_amt_owed_first_visit']
                if denominator or data[loc_id][i]['pps_total_amt_paid']:
                    month_value = self.percent_fn(
                        data[loc_id][i]['pps_total_amt_paid'],
                        denominator,
                        nominal_denominator=100,
                    )
                    row.append({
                        'html': month_value,
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                    })
            cell, value_partials_per_loc_id = self.get_recovery_rate_by_pps_in_location(data[loc_id])
            row.append(cell)
            value_partials[loc_id] = value_partials_per_loc_id
            rows.append(row)
        return rows, value_partials

    def aggregate_recovery_rate_by_pps_per_month_from_pps_to_higher_location(self, data, pps_id_per_higher_loc_id):
        agg_data = {}
        for pps_id, values in data.items():
            higher_loc_id = pps_id_per_higher_loc_id[pps_id]
            if higher_loc_id not in agg_data:
                agg_data[higher_loc_id] = []
                for i in range(len(self.months)):
                    agg_data[higher_loc_id].append({
                        'delivery_amt_owed_first_visit': 0,
                        'pps_total_amt_owed': 0,
                        'pps_total_amt_paid': 0,
                        'delivery_amt_owed': 0,
                    })
            for month_index in range(len(self.months)):
                agg_data[higher_loc_id][month_index]['delivery_amt_owed_first_visit'] += \
                    values[month_index]['delivery_amt_owed_first_visit']
                agg_data[higher_loc_id][month_index]['pps_total_amt_owed'] += \
                    values[month_index]['pps_total_amt_owed']
                agg_data[higher_loc_id][month_index]['pps_total_amt_paid'] += \
                    values[month_index]['pps_total_amt_paid']
                agg_data[higher_loc_id][month_index]['delivery_amt_owed'] += \
                    values[month_index]['delivery_amt_owed']
        return agg_data

    def get_recovery_rate_by_pps_per_month(self, records):
        data = {}
        loc_names = {}
        pps_id_per_higher_loc_id = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_precise']):
                continue
            if record['pps_id'] not in data:
                data[record['pps_id']] = []
                for i in range(len(self.months)):
                    data[record['pps_id']].append({
                        'delivery_amt_owed_first_visit': 0,
                        'real_date_precise_first': None,
                        'pps_total_amt_owed': 0,
                        'pps_total_amt_paid': 0,
                        'delivery_amt_owed': 0,
                    })
                if self.loc_id == 'pps_id':
                    loc_names[record[self.loc_id]] = record[self.loc_name]
                else:
                    if record[self.loc_id] not in loc_names:
                        loc_names[record[self.loc_id]] = record[self.loc_name]
                    pps_id_per_higher_loc_id[record['pps_id']] = record[self.loc_id]
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date_precise'])
            if record.get('delivery_amt_owed') is not None:
                if data[record['pps_id']][month_index]['real_date_precise_first'] is None or \
                        record['real_date_precise'] < \
                        data[record['pps_id']][month_index]['real_date_precise_first']:
                    data[record['pps_id']][month_index]['pps_total_amt_owed'] = record['pps_total_amt_owed']
                    data[record['pps_id']][month_index]['real_date_precise_first'] = \
                        record['real_date_precise']
                    data[record['pps_id']][month_index]['delivery_amt_owed_first_visit'] = \
                        record['delivery_amt_owed']
                if record['pps_total_amt_paid']:
                    data[record['pps_id']][month_index]['pps_total_amt_paid'] += record['pps_total_amt_paid']
                data[record['pps_id']][month_index]['delivery_amt_owed'] += record['delivery_amt_owed']
        if self.loc_id != 'pps_id':
            agg_data = self.aggregate_recovery_rate_by_pps_per_month_from_pps_to_higher_location(
                data, pps_id_per_higher_loc_id)
            return loc_names, agg_data
        return loc_names, data

    @property
    def rows(self):
        records = self.get_data()
        loc_names, data = self.get_recovery_rate_by_pps_per_month(records)
        rows, value_partials = self.parse_recovery_rate_by_pps_to_rows(loc_names, data)
        self.total_row = self.calculate_total_row(data, value_partials)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = super(RecoveryRateByPPSData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen'))
        return headers


class RecoveryRateByDistrictData(LogisticienDataSource):
    slug = 'taux_de_recouvrement_au_niveau_du_district'
    comment = 'Somme des montants payés sur total dû'
    title = 'Taux de Recouvrement au niveau du District'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, data):
        if 'region_id' in self.config and self.config['region_id']:
            total_row = [{
                'html': 'Taux par Région',
            }]
        elif 'district_id' in self.config and self.config['district_id']:
            total_row = [{
                'html': 'Taux par District',
            }]
        else:
            total_row = [{
                'html': 'Taux par Pays',
            }]
        total_numerator = 0
        total_denominator = 0
        for i in range(len(self.months)):
            numerator = sum(
                data[loc_id][i]['montant_paye'] for loc_id in data if
                data[loc_id][i]['montant_reel_a_payer']
            )
            denominator = sum(
                data[loc_id][i]['montant_reel_a_payer'] for loc_id in data if
                data[loc_id][i]['montant_reel_a_payer']
            )
            total_numerator += numerator
            total_denominator += denominator
            total_value = self.percent_fn(
                numerator,
                denominator
            )
            if denominator:
                total_row.append({
                    'html': total_value,
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        total_value = self.percent_fn(
            total_numerator,
            total_denominator
        )
        if total_denominator:
            total_row.append({
                'html': total_value,
            })
        else:
            total_row.append({
                'html': 'pas de données',
            })
        return total_row

    @property
    def group_by(self):
        return ['date_echeance', 'district_id', 'district_name']

    @property
    def columns(self):
        columns = [
            DatabaseColumn("District ID", SimpleColumn('district_id')),
            DatabaseColumn("District Name", SimpleColumn('district_name')),
            DatabaseColumn("Date", SimpleColumn('date_echeance')),
            DatabaseColumn("Sum of the amounts paid by the district", SumColumn('montant_paye')),
            DatabaseColumn("Total amount owed by the district to PNA", SumColumn('montant_reel_a_payer')),
        ]
        return columns

    def get_recovery_rate_by_district_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month['montant_reel_a_payer']:
                numerator += data_in_month['montant_paye']
                denominator += data_in_month['montant_reel_a_payer']
        if denominator:
            value = self.percent_fn(
                numerator,
                denominator,
            )
            return {
                'html': value,
            }
        else:
            return {
                'html': 'pas de données',
            }

    def parse_recovery_rate_by_district_to_rows(self, loc_names, data):
        rows = []
        for loc_id in data:
            row = [{
                'html': loc_names[loc_id],
            }]
            for i in range(len(self.months)):
                if data[loc_id][i]['montant_reel_a_payer']:
                    month_value = self.percent_fn(
                        data[loc_id][i]['montant_paye'],
                        data[loc_id][i]['montant_reel_a_payer']
                    )
                    row.append({
                        'html': month_value,
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                    })
            row.append(self.get_recovery_rate_by_district_in_location(data[loc_id]))
            rows.append(row)
        return rows

    def get_recovery_rate_by_district_per_month(self, records):
        data = defaultdict(list)
        loc_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['date_echeance']):
                continue
            if record['district_id'] not in data:
                for i in range(len(self.months)):
                    data[record['district_id']].append(defaultdict(int))
                loc_names[record['district_id']] = record['district_name']
            month_index = self.get_index_of_month_in_selected_data_range(record['date_echeance'])
            if self.denominator_exists(record['montant_reel_a_payer']):
                if record['montant_paye']:
                    data[record['district_id']][month_index]['montant_paye'] += \
                        record['montant_paye']['html']
                data[record['district_id']][month_index]['montant_reel_a_payer'] += \
                    record['montant_reel_a_payer']['html']
        return loc_names, data

    @property
    def rows(self):
        records = self.get_data()
        district_names, data = self.get_recovery_rate_by_district_per_month(records)
        self.total_row = self.calculate_total_row(data)
        rows = self.parse_recovery_rate_by_district_to_rows(district_names, data)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = super(RecoveryRateByDistrictData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen'))
        return headers


class RuptureRateByPPSData(VisiteDeLOperateurDataSource):
    slug = 'taux_de_rupture_par_pps'
    comment = 'Nombre de produits en rupture sur le nombre total de produits du PPS'
    title = 'Taux de Rupture par PPS'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, data):
        if 'region_id' in self.config and self.config['region_id']:
            total_row = [{
                'html': 'Taux par Région',
            }]
        elif 'district_id' in self.config and self.config['district_id']:
            total_row = [{
                'html': 'Taux par District',
            }]
        elif 'pps_id' in self.config and self.config['pps_id']:
            total_row = [{
                'html': '',
            }]
        else:
            total_row = [{
                'html': 'Taux par Pays',
            }]
        total_numerator = 0
        total_denominator = 0
        for i in range(len(self.months)):
            numerator = sum(
                data[loc_id][i]['nb_products_stockout'] for loc_id in data if
                data[loc_id][i]['count_products_select']
            )
            denominator = sum(
                data[loc_id][i]['count_products_select'] for loc_id in data if
                data[loc_id][i]['count_products_select']
            )
            total_numerator += numerator
            total_denominator += denominator
            total_value = self.percent_fn(
                numerator,
                denominator
            )
            if denominator:
                total_row.append({
                    'html': total_value,
                    'style': 'color: red' if self.cell_value_bigger_than(total_value, 2) else '',
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        total_value = self.percent_fn(
            total_numerator,
            total_denominator
        )
        if total_denominator:
            total_row.append({
                'html': total_value,
                'style': 'color: red' if self.cell_value_bigger_than(total_value, 2) else '',
            })
        else:
            total_row.append({
                'html': 'pas de données',
            })
        return total_row

    @property
    def group_by(self):
        return ['doc_id', 'real_date', 'pps_id', 'pps_name', 'nb_products_stockout', 'count_products_select']

    @property
    def columns(self):
        columns = [
            DatabaseColumn("DOC ID", SimpleColumn('doc_id')),
            DatabaseColumn("PPS ID", SimpleColumn('pps_id')),
            DatabaseColumn("PPS Name", SimpleColumn('pps_name')),
            DatabaseColumn("Date", SimpleColumn('real_date')),
            DatabaseColumn("Number of stockout products", SimpleColumn('nb_products_stockout')),
            DatabaseColumn("Number of products in pps", SimpleColumn('count_products_select')),
        ]
        return columns

    def get_average_rupture_rate_in_location(self, data_per_localization):
        numerator = 0
        denominator = 0
        for data_in_month in data_per_localization:
            if data_in_month and data_in_month['count_products_select']:
                numerator += data_in_month['nb_products_stockout']
                denominator += data_in_month['count_products_select']
        if denominator:
            value = self.percent_fn(
                numerator,
                denominator,
            )
            return {
                'html': value,
                'style': 'color: red' if self.cell_value_bigger_than(value, 2) else '',
            }
        else:
            return {
                'html': 'pas de données',
            }

    def parse_rupture_rate_to_rows(self, pps_names, data):
        rows = []
        for pps_id in data:
            row = [{
                'html': pps_names[pps_id],
            }]
            for i in range(len(self.months)):
                if data[pps_id][i]['count_products_select']:
                    month_value = self.percent_fn(
                        data[pps_id][i]['nb_products_stockout'],
                        data[pps_id][i]['count_products_select']
                    )
                    row.append({
                        'html': month_value,
                        'style': 'color: red' if self.cell_value_bigger_than(month_value, 2) else '',
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                    })
            row.append(self.get_average_rupture_rate_in_location(data[pps_id]))
            rows.append(row)
        return rows

    def get_rupture_rate_per_month(self, records):
        data = defaultdict(list)
        pps_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date']):
                continue
            if record['pps_id'] not in data:
                for i in range(len(self.months)):
                    data[record['pps_id']].append(defaultdict(int))
                pps_names[record['pps_id']] = record['pps_name']
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date'])
            if record['count_products_select']:
                if record['nb_products_stockout']:
                    data[record['pps_id']][month_index]['nb_products_stockout'] += \
                        record['nb_products_stockout']
                data[record['pps_id']][month_index]['count_products_select'] += record['count_products_select']
        return pps_names, data

    @property
    def rows(self):
        records = self.get_data()
        pps_names, data = self.get_rupture_rate_per_month(records)
        self.total_row = self.calculate_total_row(data)
        rows = self.parse_rupture_rate_to_rows(pps_names, data)
        return sorted(rows, key=lambda x: x[0])

    @cached_property
    def loc_id(self):
        return 'pps_id'

    @property
    def headers(self):
        headers = super(RuptureRateByPPSData, self).headers
        headers.add_column(DataTablesColumn('Taux moyen'))
        return headers


class SatisfactionRateAfterDeliveryData(VisiteDeLOperateurPerProductDataSource):
    slug = 'taux_de_satisfaction_apres_livraison'
    comment = 'produits proposés sur produits livrés'
    title = 'Taux de satisfaction (après livraison)'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, products):
        total_row = ['Total (%)']
        for i in range(len(self.months)):
            numerator = sum(
                products[product_id][i]['amt_delivered_convenience'] for product_id in products if
                products[product_id][i]['ideal_topup']
            )
            denominator = sum(
                products[product_id][i]['ideal_topup'] for product_id in products if
                products[product_id][i]['ideal_topup']
            )
            if denominator:
                month_value = self.percent_fn(
                    numerator,
                    denominator
                )
                if self.cell_value_less_than(month_value, 90):
                    style = 'color: red'
                elif self.cell_value_bigger_than(month_value, 100):
                    style = 'color: orange'
                else:
                    style = ''
                total_row.append({
                    'html': month_value,
                    'style': style,
                })
            else:
                total_row.append({
                    'html': 'pas de données',
                })
        return total_row

    @property
    def group_by(self):
        return ['real_date_repeat', 'product_id', 'product_name']

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Date", SimpleColumn('real_date_repeat')),
            DatabaseColumn("Product ID", SimpleColumn('product_id')),
            DatabaseColumn("Product Name", SimpleColumn('product_name')),
            DatabaseColumn("Quantity of the product delivered", SumColumn('amt_delivered_convenience')),
            DatabaseColumn("Quantity of the product  suggested", SumColumn('ideal_topup')),
        ]
        return columns

    def get_product_satisfaction_rate_per_month(self, records):
        data = defaultdict(list)
        product_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_repeat']):
                continue
            if record['product_id'] not in data:
                for i in range(len(self.months)):
                    data[record['product_id']].append(defaultdict(int))
                product_names[record['product_id']] = record['product_name']
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date_repeat'])
            if self.denominator_exists(record['ideal_topup']):
                if record['amt_delivered_convenience']:
                    data[record['product_id']][month_index]['amt_delivered_convenience'] += \
                        record['amt_delivered_convenience']['html']
                data[record['product_id']][month_index]['ideal_topup'] += record['ideal_topup']['html']
        return product_names, data

    def parse_satisfaction_rate_to_rows(self, product_names, data):
        rows = []
        for product_id in data:
            row = [product_names[product_id]]
            for i in range(len(self.months)):
                if data[product_id][i]['ideal_topup']:
                    month_value = self.percent_fn(
                        data[product_id][i]['amt_delivered_convenience'],
                        data[product_id][i]['ideal_topup']
                    )
                    if self.cell_value_less_than(month_value, 90):
                        style = 'color: red'
                    elif self.cell_value_bigger_than(month_value, 100):
                        style = 'color: orange'
                    else:
                        style = ''
                    row.append({
                        'html': month_value,
                        'style': style,
                    })
                else:
                    row.append({
                        'html': 'pas de données',
                        'style': '',
                    })
            rows.append(row)
        return rows

    @property
    def rows(self):
        records = self.get_data()
        product_names, data = self.get_product_satisfaction_rate_per_month(records)
        self.total_row = self.calculate_total_row(data)
        rows = self.parse_satisfaction_rate_to_rows(product_names, data)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('Produit'))
        for month in self.month_headers():
            headers.add_column(month)
        return headers


class ValuationOfPNAStockPerProductData(VisiteDeLOperateurPerProductDataSource):
    slug = 'valeur_des_stocks_pna_disponible_chaque_produit'
    comment = 'Valeur des stocks PNA disponible (chaque produit)'
    title = 'Valeur des stocks PNA disponible (chaque produit)'
    show_total = True
    custom_total_calculate = True

    def calculate_total_row(self, records):
        total_row = []
        data = defaultdict(int)
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_repeat']):
                continue
            month_index = self.get_index_of_month_in_selected_data_range(record['real_date_repeat'])
            if record['final_pna_stock_valuation']:
                data[month_index] += record['final_pna_stock_valuation']['html']

        total_row.append('Total (CFA)')
        for month_index in range(len(self.months)):
            if data.get(month_index) is not None:
                total_row.append(
                    '{:,}'.format(data[month_index]).replace(',', '.')
                )
            else:
                total_row.append('pas de données')
        return total_row

    @property
    def group_by(self):
        return ['real_date_repeat', 'product_id', 'product_name']

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Date", SimpleColumn('real_date_repeat')),
            DatabaseColumn("Product ID", SimpleColumn('product_id')),
            DatabaseColumn("Product Name", SimpleColumn('product_name')),
            DatabaseColumn("Products stock valuation", SumColumn('final_pna_stock_valuation')),
        ]
        return columns

    def get_product_valuation_of_pna_stock_per_month(self, records):
        data = defaultdict(list)
        product_names = {}
        for record in records:
            if not self.date_in_selected_date_range(record['real_date_repeat']):
                continue
            if record['final_pna_stock_valuation']:
                if record['product_id'] not in data:
                    for i in range(len(self.months)):
                        data[record['product_id']].append(defaultdict(int))
                    product_names[record['product_id']] = record['product_name']
                month_index = self.get_index_of_month_in_selected_data_range(record['real_date_repeat'])
                data[record['product_id']][month_index]['final_pna_stock_valuation'] += \
                    record['final_pna_stock_valuation']['html']
        return product_names, data

    @property
    def rows(self):
        records = self.get_data()
        product_names, data = self.get_product_valuation_of_pna_stock_per_month(records)

        rows = []
        for product_id in data:
            row = [product_names[product_id]]
            row.extend([
                '{:,}'.format(value['final_pna_stock_valuation']).replace(',', '.')
                if value.get('final_pna_stock_valuation') is not None
                else
                'pas de données'
                for value in data[product_id]
            ])
            rows.append(row)
        self.total_row = self.calculate_total_row(records)
        return sorted(rows, key=lambda x: x[0])

    @property
    def headers(self):
        headers = DataTablesHeader(DataTablesColumn('Produit'))
        for month in self.month_headers():
            headers.add_column(month)
        return headers
