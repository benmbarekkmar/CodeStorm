import os, sys
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'scratch_settings')
settings.configure(USE_I18N=True, BASE_DIR=os.getcwd(), INSTALLED_APPS=['django.contrib.contenttypes', 'django.contrib.auth'], DATABASES={})
django.setup()
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
