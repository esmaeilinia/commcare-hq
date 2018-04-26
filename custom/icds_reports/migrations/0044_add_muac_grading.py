# -*- coding: utf-8 -*-
# flake8: noqa
# Generated by Django 1.11.12 on 2018-04-24 16:09
from __future__ import absolute_import
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('icds_reports', '0043_merge_20180424_1426'),
    ]

    operations = [
        migrations.AddField(
            model_name='aggregategrowthmonitoringforms',
            name='muac_grading',
            field=models.PositiveSmallIntegerField(help_text='Last recorded muac_grading before end of this month', null=True),
        ),
        migrations.AddField(
            model_name='aggregategrowthmonitoringforms',
            name='muac_grading_last_recorded',
            field=models.DateTimeField(help_text='Time when muac_grading was last recorded', null=True),
        ),
        migrations.AlterField(
            model_name='aggregategrowthmonitoringforms',
            name='zscore_grading_hfa',
            field=models.PositiveSmallIntegerField(help_text='Last recorded zscore_grading_hfa before end of this month', null=True),
        ),
        migrations.AlterField(
            model_name='aggregategrowthmonitoringforms',
            name='zscore_grading_wfa',
            field=models.PositiveSmallIntegerField(help_text='Last recorded zscore_grading_wfa before end of this month', null=True),
        ),
        migrations.AlterField(
            model_name='aggregategrowthmonitoringforms',
            name='zscore_grading_wfh',
            field=models.PositiveSmallIntegerField(help_text='Last recorded zscore_grading_wfh before end of this month', null=True),
        ),
    ]