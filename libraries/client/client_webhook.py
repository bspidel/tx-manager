from __future__ import print_function, unicode_literals
import urllib
import copy
import os
import tempfile
import requests
import json
from datetime import datetime
from libraries.door43_tools.linter_messaging import LinterMessaging
from libraries.general_tools.file_utils import unzip, write_file, add_contents_to_zip, remove_tree
from libraries.general_tools.url_utils import download_file
from libraries.resource_container.ResourceContainer import RC, BIBLE_RESOURCE_TYPES
from libraries.client.preprocessors import do_preprocess
from libraries.models.manifest import TxManifest
from libraries.app.app import App
from libraries.models.job import TxJob


class ClientWebhook(object):

    def __init__(self, commit_data=None):
        """
        :param dict commit_data:
        """
        self.commit_data = commit_data
        if App.pre_convert_bucket:
            # we use us-west-2 for our s3 buckets
            self.source_url_base = 'https://s3-us-west-2.amazonaws.com/{0}'.format(App.pre_convert_bucket)
        else:
            self.source_url_base = None
        self.run_linter_function = '{0}tx_run_linter'.format(App.prefix)
        # move everything down one directory level for simple delete
        self.intermediate_dir = 'tx-manager'
        self.base_temp_dir = os.path.join(tempfile.gettempdir(), self.intermediate_dir)

    def process_webhook(self):
        try:
            os.makedirs(self.base_temp_dir)
        except:
            pass

        commit_id = self.commit_data['after']
        commit = None
        for commit in self.commit_data['commits']:
            if commit['id'] == commit_id:
                break
        commit_id = commit_id[:10]  # Only use the short form

        commit_url = commit['url']
        commit_message = commit['message']

        if App.gogs_url not in commit_url:
            raise Exception('Repos can only belong to {0} to use this webhook client.'.format(App.gogs_url))

        repo_name = self.commit_data['repository']['name']
        repo_owner = self.commit_data['repository']['owner']['username']
        compare_url = self.commit_data['compare_url']

        if 'pusher' in self.commit_data:
            pusher = self.commit_data['pusher']
        else:
            pusher = {'username': commit['author']['username']}
        pusher_username = pusher['username']

        # 1) Download and unzip the repo files
        repo_dir = self.get_repo_files(commit_url, repo_name)

        # Get the resource container
        rc = RC(repo_dir, repo_name)

        # Save manifest to manifest table
        manifest_data = {
            'repo_name': repo_name,
            'user_name': repo_owner,
            'lang_code': rc.resource.language.identifier,
            'resource_id': rc.resource.identifier,
            'resource_type': rc.resource.type,
            'title': rc.resource.title,
            'last_updated': datetime.utcnow(),
            'manifest': json.dumps(rc.as_dict()),
        }
        # First see if manifest already exists in DB and update it if it is
        tx_manifest = App.db.query(TxManifest).filter_by(repo_name=repo_name, user_name=repo_owner).first()
        if tx_manifest:
            for key, value in manifest_data.iteritems():
                setattr(tx_manifest, key, value)
            App.logger.debug('Updating manifest in manifest table: {0}'.format(manifest_data))
        else:
            tx_manifest = TxManifest(**manifest_data)
            App.logger.debug('Inserting manifest into manifest table: {0}'.format(tx_manifest))
            App.db.add(tx_manifest)
        App.db.commit()

        # Preprocess the files
        output_dir = tempfile.mkdtemp(dir=self.base_temp_dir, prefix='output_')
        results, preprocessor = do_preprocess(rc, repo_dir, output_dir)

        # 3) Zip up the massaged files
        # commit_id is a unique ID for this lambda call, so using it to not conflict with other requests
        zip_filepath = tempfile.mktemp(dir=self.base_temp_dir, suffix='.zip')
        App.logger.debug('Zipping files from {0} to {1}...'.format(output_dir, zip_filepath))
        add_contents_to_zip(zip_filepath, output_dir)
        App.logger.debug('finished.')

        # 4) Upload zipped file to the S3 bucket
        file_key = self.upload_zip_file(commit_id, zip_filepath)

        if not preprocessor.is_multiple_jobs():
            # Send job request to tx-manager
            identifier, job = self.send_job_request_to_tx_manager(commit_id, file_key, rc, repo_name, repo_owner)

            # Compile data for build_log.json
            build_log_json = self.create_build_log(commit_id, commit_message, commit_url, compare_url, job,
                                                   pusher_username, repo_name, repo_owner)

            # Upload build_log.json to S3:
            s3_commit_key = 'u/{0}'.format(identifier)
            self.clear_commit_directory_in_cdn(s3_commit_key)
            self.upload_build_log_to_s3(build_log_json, s3_commit_key)

            # Download the project.json file for this repo (create it if doesn't exist) and update it
            self.update_project_json(commit_id, job, repo_name, repo_owner)

            # Send lint request
            lint_results = self.send_lint_request_to_run_linter(job, rc, commit_url)
            job = TxJob(job.job_id)
            if 'success' in lint_results and lint_results['success']:
                job.warnings += lint_results['warnings']
                job.update('warnings')
                # Upload build_log.json to S3 again:
                build_log_json = self.create_build_log(commit_id, commit_message, commit_url, compare_url, job,
                                                       pusher_username, repo_name, repo_owner)
                self.upload_build_log_to_s3(build_log_json, s3_commit_key)

            remove_tree(self.base_temp_dir)  # cleanup

            if len(job.errors) > 0:
                raise Exception('; '.join(job.errors))
            else:
                return build_log_json

        # -------------------------
        # multiple book project
        # -------------------------

        books = preprocessor.get_book_list()
        App.logger.debug('Splitting job into separate parts for books: ' + ','.join(books))
        errors = []
        build_logs = []
        jobs = []
        job = None

        master_identifier = self.create_new_identifier(repo_owner, repo_name, commit_id)
        master_s3_commit_key = 'u/{0}'.format(master_identifier)
        self.clear_commit_directory_in_cdn(master_s3_commit_key)

        book_count = len(books)
        last_job_id = '0'

        linter_queue = LinterMessaging(App.linter_messaging_name)
        source_urls = self.clear_out_any_old_messages(linter_queue, book_count, books, file_key)
        linter_payload = {
            'linter_messaging_name': App.linter_messaging_name
        }

        build_log_json = {}
        for i in range(0, book_count):
            book = books[i]
            part_id = '{0}_of_{1}'.format(i, book_count)

            App.logger.debug('Adding job for {0} part {1}'.format(book, part_id))

            # Send job request to tx-manager
            file_key_multi = self.build_multipart_source(file_key, book)
            identifier, job = self.send_job_request_to_tx_manager(commit_id, file_key_multi, rc, repo_name, repo_owner,
                                                                  count=book_count, part=i, book=book)

            # Send lint request to tx-manager
            linter_payload['single_file'] = book
            self.send_lint_request_to_run_linter(job, rc, file_key_multi, extra_data=linter_payload, async=True)

            jobs.append(job)
            last_job_id = job.job_id

            build_log_json = self.create_build_log(commit_id, commit_message, commit_url, compare_url, job,
                                                   pusher_username, repo_name, repo_owner)
            part = str(i)
            if len(book) > 0:
                build_log_json['book'] = book
                build_log_json['part'] = part

            # Upload build_log.json to S3:
            self.upload_build_log_to_s3(build_log_json, master_s3_commit_key, part + "/")

            errors += job.errors
            build_logs.append(build_log_json)

        # Download the project.json file for this repo (create it if doesn't exist) and update it
        self.update_project_json(commit_id, jobs[0], repo_name, repo_owner)

        source_url = self.source_url_base + "/preconvert/" + commit_id + '.zip'
        build_logs_json = copy.copy(build_log_json)
        build_logs_json['multiple'] = True
        build_logs_json['build_logs'] = build_logs
        build_logs_json['job_id'] = last_job_id
        build_logs_json['source'] = source_url
        errors = []
        warnings = []
        for i in range(0, book_count):
            build_log = build_logs[i]
            errors += build_log['errors']
            warnings += build_log['warnings']
        build_logs_json['errors'] = errors
        build_logs_json['warnings'] = warnings

        # Upload build_log.json to S3 before waiting for linters to complete:
        self.upload_build_log_to_s3(build_logs_json, master_s3_commit_key)

        callback = (lambda lint_data: self.update_job_with_linter_data(build_logs_json, lint_data))

        # process results of each linter when finished by calling callback
        success = linter_queue.wait_for_lint_jobs(source_urls, callback=callback, checking_interval=1,
                                                  timeout=180)  # wait up to 3 minutes
        if not success:
            for source_url in linter_queue.get_unfinished_lint_jobs():
                msg = "Linter didn't complete for file: " + source_url
                build_logs_json['errors'].append(msg)
                App.logger.error(msg)
                if job:
                    job.errors.append(msg)
            if job:
                job.update('errors')

        # Upload build_log.json to S3 again:
        self.upload_build_log_to_s3(build_logs_json, master_s3_commit_key)

        App.logger.debug("Final json: " + str(build_logs_json))

        remove_tree(self.base_temp_dir)  # cleanup

        if len(errors) > 0:
            raise Exception('; '.join(errors))
        else:
            return build_logs_json

    @staticmethod
    def update_job_with_linter_data(build_logs_json, lint_data):
        source = LinterMessaging.get_source_url_from_data(lint_data)

        if ('success' not in lint_data) or not lint_data['success']:
            msg = "Linter failed for source: " + source
            build_logs_json['warnings'].append(msg)
            App.logger.debug(msg)
        else:
            App.logger.debug("Linter {0} results:\n{1}".format(source, str(lint_data)))

        has_warnings = False
        if 'warnings' in lint_data and len(lint_data['warnings']):
            App.logger.debug("Linter {0} Warnings:\n{1}".format(source, '\n'.join(lint_data['warnings'])))
            build_logs_json['warnings'] += lint_data['warnings']
            has_warnings = True

        if has_warnings:
            msg = "Linter {0} has Warnings!".format(source)
            build_logs_json['log'].append(msg)
        else:
            msg = "Linter {0} completed with no warnings".format(source)
            build_logs_json['log'].append(msg)

    def clear_out_any_old_messages(self, linter_queue, book_count, books, file_key):
        source_urls = []
        for i in range(0, book_count):
            book = books[i]
            file_key_multi = self.build_multipart_source(file_key, book)
            source_url = self.source_url_base + "/" + file_key_multi
            source_urls.append(source_url)
        linter_queue.clear_old_lint_jobs(source_urls, 2)
        return source_urls

    @staticmethod
    def build_multipart_source(file_key, book):
        params = urllib.urlencode({'convert_only': book})
        source_url = '{0}?{1}'.format(file_key, params)
        return source_url

    @staticmethod
    def clear_commit_directory_in_cdn(s3_commit_key):
        # clear out the commit directory in the cdn bucket for this project revision
        for obj in App.cdn_s3_handler.get_objects(prefix=s3_commit_key):
            App.logger.debug('Removing file: ' + obj.key)
            App.cdn_s3_handler.delete_file(obj.key)

    def upload_build_log_to_s3(self, build_log_json, s3_commit_key, part=''):
        build_log_file = os.path.join(self.base_temp_dir, 'build_log.json')
        write_file(build_log_file, build_log_json)
        upload_key = '{0}/{1}build_log.json'.format(s3_commit_key, part)
        App.logger.debug('Saving build log to ' + upload_key)
        App.cdn_s3_handler.upload_file(build_log_file, upload_key)
        # App.logger.debug('build log contains: ' + json.dumps(build_log_json))

    @staticmethod
    def create_build_log(commit_id, commit_message, commit_url, compare_url, job, pusher_username, repo_name,
                         repo_owner):
        build_log_json = job.get_db_data()
        build_log_json['repo_name'] = repo_name
        build_log_json['repo_owner'] = repo_owner
        build_log_json['commit_id'] = commit_id
        build_log_json['committed_by'] = pusher_username
        build_log_json['commit_url'] = commit_url
        build_log_json['compare_url'] = compare_url
        build_log_json['commit_message'] = commit_message
        return build_log_json

    def update_project_json(self, commit_id, job, repo_name, repo_owner):
        project_json_key = 'u/{0}/{1}/project.json'.format(repo_owner, repo_name)
        project_json = App.cdn_s3_handler.get_json(project_json_key)
        project_json['user'] = repo_owner
        project_json['repo'] = repo_name
        project_json['repo_url'] = 'https://git.door43.org/{0}/{1}'.format(repo_owner, repo_name)
        commit = {
            'id': commit_id,
            'created_at': job.created_at,
            'status': job.status,
            'success': job.success,
            'started_at': None,
            'ended_at': None
        }
        if 'commits' not in project_json:
            project_json['commits'] = []
        commits = []
        for c in project_json['commits']:
            if c['id'] != commit_id:
                commits.append(c)
        commits.append(commit)
        project_json['commits'] = commits
        project_file = os.path.join(self.base_temp_dir, 'project.json')
        write_file(project_file, project_json)
        App.cdn_s3_handler.upload_file(project_file, project_json_key)

    @staticmethod
    def upload_zip_file(commit_id, zip_filepath):
        file_key = 'preconvert/{0}.zip'.format(commit_id)
        App.logger.debug('Uploading {0} to {1}/{2}...'.format(zip_filepath, App.pre_convert_bucket, file_key))
        try:
            App.pre_convert_s3_handler.upload_file(zip_filepath, file_key)
        except Exception as e:
            App.logger.error('Failed to upload zipped repo up to server')
            App.logger.exception(e)
        finally:
            App.logger.debug('finished.')

        return file_key

    def get_repo_files(self, commit_url, repo_name):
        temp_dir = tempfile.mkdtemp(dir=self.base_temp_dir, prefix='{0}_'.format(repo_name))
        self.download_repo(commit_url, temp_dir)
        repo_dir = os.path.join(temp_dir, repo_name.lower())
        if not os.path.isdir(repo_dir):
            repo_dir = temp_dir

        return repo_dir

    def send_job_request_to_tx_manager(self, commit_id, file_key, rc, repo_name, repo_owner,
                                       count=0, part=0, book=None, warnings=None):
        source_url = self.source_url_base + "/" + file_key
        callback_url = App.api_url + '/client/callback'
        tx_manager_job_url = App.api_url + '/tx/job'

        identifier = self.create_new_identifier(repo_owner, repo_name, commit_id, count, part, book)

        payload = {
            "identifier": identifier,
            "gogs_user_token": App.gogs_user_token,
            "resource_type": rc.resource.identifier,
            "input_format": rc.resource.file_ext,
            "output_format": "html",
            "source": source_url,
            "callback": callback_url,
            "warning": warnings
        }
        return self.add_payload_to_tx_converter(callback_url, identifier, payload, rc, source_url, tx_manager_job_url)

    @staticmethod
    def create_new_identifier(repo_owner, repo_name, commit_id, count=0, part=0, book=None):
        if not count:
            identifier = "{0}/{1}/{2}".format(repo_owner, repo_name,
                                              commit_id)  # The way to know which repo/commit goes to this job request
        else:  # if this is part of a multipart job
            # The way to know which repo/commit goes to this job request
            identifier = "{0}/{1}/{2}/{3}/{4}/{5}".format(repo_owner, repo_name, commit_id, count, part, book)
        return identifier

    @staticmethod
    def add_payload_to_tx_converter(callback_url, identifier, payload, rc, source_url, tx_manager_job_url):
        headers = {"content-type": "application/json"}
        App.logger.debug('Making request to tX-Manager URL {0} with payload:'.format(tx_manager_job_url))
        # remove token from printout, so it will not show in integration testing logs on Travis, etc.
        log_payload = payload.copy()
        log_payload["gogs_user_token"] = "DUMMY"
        App.logger.debug(log_payload)
        response = requests.post(tx_manager_job_url, json=payload, headers=headers)
        App.logger.debug('finished.')

        # Fake job in case tx-manager returns an error, can still build the build_log.json
        job = TxJob({
            'identifier': identifier,
            'resource_type': rc.resource.identifier,
            'input_format': rc.resource.file_ext,
            'output_format': 'html',
            'source': source_url,
            'callback': callback_url,
            'message': 'Conversion started...',
            'status': 'requested',
            'success': None,
            'created_at': datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            'log': [],
            'warnings': [],
            'errors': []
        })
        if response.status_code != requests.codes.ok:
            job.status = 'failed'
            job.success = False
            job.message = 'Failed to convert'

            if response.text:
                # noinspection PyBroadException
                try:
                    json_data = json.loads(response.text)
                    if 'errorMessage' in json_data:
                        error = json_data['errorMessage']
                        if error.startswith('Bad Request: '):
                            error = error[len('Bad Request: '):]
                        job.errors.append(error)
                except:
                    pass
        else:
            json_data = json.loads(response.text)

            if 'job' not in json_data:
                job.status = 'failed'
                job.success = False
                job.message = 'Failed to convert'
                job.errors.append('tX Manager did not return any info about the job request.')
            else:
                job.populate(json_data['job'])
        return identifier, job

    def send_lint_request_to_run_linter(self, job, rc, commit_url, extra_data=None, async=False):
        job_data = {
            'job_id': job.job_id,
            'resource_id': rc.resource.identifier,
            'commit_data': self.commit_data,
        }
        if extra_data:
            for k in extra_data:
                job_data[k] = extra_data[k]
        payload = {
            'data': job_data,
            'vars': {
                'prefix': App.prefix,
            }
        }

        if job.resource_type in BIBLE_RESOURCE_TYPES or job.resource_type == 'obs':
            # Need to give the massaged source since it maybe was in chunks originally
            payload['data']['source_url'] = job.source
        else:
            payload['data']['source_url'] = commit_url.replace('commit', 'archive') + '.zip'
        return self.send_payload_to_run_linter(payload, async=async)

    def send_payload_to_run_linter(self, payload, async=False):
        App.logger.debug('Making request linter lambda with payload:')
        App.logger.debug(payload)
        response = App.lambda_handler.invoke(function_name=self.run_linter_function, payload=payload, async=async)
        App.logger.debug('finished.')
        if 'Payload' in response:
            if async:
                results = {'success': True, 'status': 'queued'}
                return results
            return json.loads(response['Payload'].read())
        else:
            return {'success': False, 'warnings': []}

    def download_repo(self, commit_url, repo_dir):
        """
        Downloads and unzips a git repository from Github or git.door43.org

        :param str|unicode commit_url: The URL of the repository to download
        :param str|unicode repo_dir:   The directory where the downloaded file should be unzipped
        :return: None
        """
        repo_zip_url = commit_url.replace('commit', 'archive') + '.zip'
        repo_zip_file = os.path.join(self.base_temp_dir, repo_zip_url.rpartition(os.path.sep)[2])

        try:
            App.logger.debug('Downloading {0}...'.format(repo_zip_url))

            # if the file already exists, remove it, we want a fresh copy
            if os.path.isfile(repo_zip_file):
                os.remove(repo_zip_file)

            download_file(repo_zip_url, repo_zip_file)
        finally:
            App.logger.debug('finished.')

        try:
            App.logger.debug('Unzipping {0}...'.format(repo_zip_file))
            unzip(repo_zip_file, repo_dir)
        finally:
            App.logger.debug('finished.')

        # clean up the downloaded zip file
        if os.path.isfile(repo_zip_file):
            os.remove(repo_zip_file)
