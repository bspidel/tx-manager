from __future__ import print_function, unicode_literals
import os
import tempfile
import traceback
from libraries.general_tools.url_utils import download_file
from libraries.general_tools.file_utils import unzip, add_contents_to_zip, remove_tree, remove
from libraries.app.app import App
from shutil import copy
from convert_logger import ConvertLogger
from abc import ABCMeta, abstractmethod


class Converter(object):
    __metaclass__ = ABCMeta

    EXCLUDED_FILES = ["license.md", "package.json", "project.json", 'readme.md']

    def __init__(self, source, resource, cdn_file=None, options=None):
        """
        :param string source:
        :param string resource:
        :param string cdn_file:
        :param dict options:
        """
        self.options = {}
        self.source = source
        self.resource = resource
        self.cdn_file = cdn_file
        self.options = options

        if not self.options:
            self.options = {}

        self.log = ConvertLogger()
        self.download_dir = tempfile.mkdtemp(prefix='download_')
        self.files_dir = tempfile.mkdtemp(prefix='files_')
        self.input_zip_file = None  # If set, won't download the repo archive. Used for testing
        self.output_dir = tempfile.mkdtemp(prefix='output_')
        self.output_zip_file = tempfile.mktemp(prefix="{0}_".format(resource), suffix='.zip')

    def close(self):
        """delete temp files"""
        remove_tree(self.download_dir)
        remove_tree(self.files_dir)
        remove_tree(self.output_dir)
        remove(self.output_zip_file)

    def __del__(self):
        self.close()

    @abstractmethod
    def convert(self):
        """
        Dummy function for converters.
        
        Returns true if the resource could be converted
        :return bool:
        """
        raise NotImplementedError()

    def run(self):
        """
        Call the converters
        """
        success = False
        try:
            if not self.input_zip_file or not os.path.exists(self.input_zip_file):
                # No input zip file yet, so we need to download the archive
                self.download_archive()
            # unzip the input archive
            App.logger.debug("Unzipping {0} to {1}".format(self.input_zip_file, self.files_dir))
            unzip(self.input_zip_file, self.files_dir)
            # convert method called
            App.logger.debug("Converting files...")
            if self.convert():
                App.logger.debug("Was able to convert {0}".format(self.resource))
                # zip the output dir to the output archive
                App.logger.debug("Adding files in {0} to {1}".format(self.output_dir, self.output_zip_file))
                add_contents_to_zip(self.output_zip_file, self.output_dir)
                remove_tree(self.output_dir)
                # upload the output archive either to cdn_bucket or to a file (no cdn_bucket)
                App.logger.debug("Uploading archive to {0}/{1}".format(App.cdn_bucket, self.cdn_file))
                self.upload_archive()
                remove(self.output_zip_file)
                App.logger.debug("Uploaded")
                success = True
            else:
                self.log.error('Resource {0} currently not supported.'.format(self.resource))
        except Exception as e:
            self.log.error('Conversion process ended abnormally: {0}'.format(e.message))
            App.logger.error('{0}: {1}'.format(str(e), traceback.format_exc()))

        result = {
            'success': success and len(self.log.logs['error']) == 0,
            'info': self.log.logs['info'],
            'warnings': self.log.logs['warning'],
            'errors': self.log.logs['error']
        }
        App.logger.debug(result)
        return result

    def download_archive(self):
        archive_url = self.source
        filename = self.source.rpartition('/')[2]
        self.input_zip_file = os.path.join(self.download_dir, filename)
        if not os.path.isfile(self.input_zip_file):
            try:
                download_file(archive_url, self.input_zip_file)
            finally:
                if not os.path.isfile(self.input_zip_file):
                    raise Exception("Failed to download {0}".format(archive_url))

    def upload_archive(self):
        if self.cdn_file and os.path.isdir(os.path.dirname(self.cdn_file)):
            copy(self.output_zip_file, self.cdn_file)
        elif App.cdn_s3_handler:
            App.cdn_s3_handler.upload_file(self.output_zip_file, self.cdn_file)
