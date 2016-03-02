from __future__ import print_function
from django.core.management.base import BaseCommand
import csv
import re
import sys
from ... import models as sa_models
from ... import forms
# for manually testing with `./manage.py shell` commandline:
# from sa_api_v2 import models as sa_models
# from sa_api_v2 import forms
import datetime
import json
from django.core.files import File
import os
import urllib

import logging
log = logging.getLogger(__name__)

csv_filepathname = sys.argv[2]

LAT_COLUMN = 'Lat'
LON_COLUMN = 'Long'

RAINGARDEN_NAME_COLUMN = 'Rain Garden Name'
IMAGE_COLUMN = 'Image'
RAINGARDEN_NUMBER_COLUMN = 'Rain Garden Number'
CONTRIBUTOR_NAME_COLUMN = 'Contributor\'s Name'
EMAIL_COLUMN = 'Email'
RAINGARDEN_SIZE_COLUMN = 'Rain garden Size (sq ft)'
RAINGARDEN_CONTRIBUTING_AREA_COLUMN = 'Contributing Area (sq ft)'
DESIGNER_COLUMN = 'Designer'
INSTALLER_COLUMN = 'Installer'
REMAIN_PRIVATE_COLUMN = 'Remain Private'
DESCRIPTION_COLUMN = 'Description'
PRIMARY_SOURCES_COLUMN = 'Primary Sources'

STREET_ADDRESS_COLUMN = 'Street Address'
CITY_COLUMN = 'City'
ZIP_COLUMN = 'Zip Code'
STATE = 'WA'


class Command(BaseCommand):
    help = 'Import a CSV file of places.'

    def handle(self, *args, **options):
        log.info('Command.handle: starting CSV import (log.info)')
        print('Command.handle: starting CSV import (print)')

        reader = csv.DictReader(open(csv_filepathname))
        i = 0
        for row in reader:
            if (i % 10 == 0):
                print("reading row", i)
            i += 1
            self.save_row(row)

    def save_row(self, row):
        lat = float(row[LAT_COLUMN])
        lon = float(row[LON_COLUMN])

        # create our data, used in Place for our dataset:
        location_type = 'raingarden'
        garden_size = validate(row[RAINGARDEN_SIZE_COLUMN])
        drainage_area = validate(row[RAINGARDEN_CONTRIBUTING_AREA_COLUMN])
        designer = validate(row[DESIGNER_COLUMN])
        installer = validate(row[INSTALLER_COLUMN])
        remain_private = row[REMAIN_PRIVATE_COLUMN]

        description = row[DESCRIPTION_COLUMN]

        # Create array of values when cell contains 'roof', 'pavement',
        #  or 'other'
        regex = re.compile(r'[, ]+')
        raw_sources = regex.split(row[PRIMARY_SOURCES_COLUMN].lower())
        possible_sources = {"driveway": "pavement",
                            "street": "pavement",
                            "roof": "roof",
                            "pavement": "pavement",
                            "garden": "other",
                            "other": "other"
                            }
        sources = set()
        for source in raw_sources:
            try:
                sources.add(possible_sources[source])
            except KeyError:
                continue
        sources = list(sources)

        street_address = row[STREET_ADDRESS_COLUMN]
        city = row[CITY_COLUMN]
        zip_code = row[ZIP_COLUMN]

        full_address = [street_address, city, STATE, zip_code]
        filtered_full_address = [x for x in full_address
                                 if (x and x != 'NULL')]
        garden_address = ", ".join(filtered_full_address)

        rain_garden_name = row[RAINGARDEN_NAME_COLUMN]

        # share_user_info_header = 'Please do not share any of my information,
        # I wish it to remain private'
        # TODO: For now, if rain gardens are private, we assume
        # all sensitive info is removed from source file
        share_user_info_header = 'Remain Private'
        remain_private = row[share_user_info_header] == 'YES'

        submitter_name = os.environ['RAIN_GARDENS_STEWARD_NAME']
        submitter_email = os.environ['RAIN_GARDENS_STEWARD_EMAIL']

        if (row[CONTRIBUTOR_NAME_COLUMN] != '' and row[EMAIL_COLUMN] != ''):
            username = row[CONTRIBUTOR_NAME_COLUMN]
            email = row[EMAIL_COLUMN]
        else:  # Use our defaults when username and email are not provided
            username = submitter_name
            email = submitter_email

        rain_garden_number = row[RAINGARDEN_NUMBER_COLUMN]

        data = {
            "rain_garden_size": garden_size,
            "designer": designer,
            "private-rain_garden_address": garden_address,
            "installer": installer,
            "contributing_area": drainage_area,
            "sources": sources,

            "description": description,
            "location_type": location_type,
            "rain_garden_name": rain_garden_name,
            "private-contributor_email": email,
            "contributor_name": username,
            "rain_garden_number": rain_garden_number,
            "remain_private": remain_private
        }
        data = json.dumps(data)

        placeForm = forms.PlaceForm({
            "data": data,
            # For geometry, using floats for lat/lon are accurate enough
            "geometry": "POINT(%f %f)" % (lon, lat),
            "created_datetime": datetime.datetime.now(),
            "updated_datetime": datetime.datetime.now(),
            "visible": True
        })
        place = placeForm.save(commit=False)

        try:
            submitter = sa_models.User.objects.get(
                username=username,
                email=email
            )
        except sa_models.User.DoesNotExist:
            submitter = sa_models.User.objects.get(
                username=os.environ['RAIN_GARDENS_STEWARD_USERNAME']
            )
        place.submitter = submitter

        try:
            dataset = sa_models.DataSet.objects.get(slug='raingardens')
        except sa_models.DataSet.DoesNotExist:
            # query for the dataset
            dataset_owner = sa_models.User.objects.get(
                username=os.environ['DATASET_OWNER_NAME'],
                email=os.environ['DATASET_OWNER_EMAIL']
            )
            dataset = sa_models.DataSet(
                slug='raingardens',
                display_name='raingardens',
                owner=dataset_owner
            )
            print("existing dataset does not exist, creating new dataset:",
                  'raingardens')
            dataset.save()

        place.dataset = dataset

        place.save()

        imageUrl = row[IMAGE_COLUMN]

        # TODO: Parallelize this!
        # TODO: Use pipe instead of saving/uploading file locally
        if imageUrl:
            file_name = "blob"
            content = urllib.urlretrieve(imageUrl, file_name)
            temp_file = File(open(content[0]))

            attachmentForm = forms.AttachmentForm({
                "created_datetime": datetime.datetime.now(),
                "updated_datetime": datetime.datetime.now(),
                "name": "my_image"
            }, {"file": temp_file})

            attachment = attachmentForm.save(commit=False)
            attachment.thing = place.submittedthing_ptr
            attachment.save()
            temp_file.close()
            os.remove(file_name)


# value must be a string
def validate(value):
    if value == 'NULL' or value.isspace():
        return ''
    else:
        return value