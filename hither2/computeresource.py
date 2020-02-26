import time
import kachery as ka
from .core import _serialize_item, _deserialize_job, _prepare_container
from ._util import _random_string
from .database import Database
from .file import File

class ComputeResource:
    def __init__(self, *, database: Database, compute_resource_id, kachery, job_handler, job_cache=None):
        self._database = database
        self._compute_resource_id = compute_resource_id
        self._kachery = kachery
        self._instance_id = _random_string(15)
        self._database = database
        self._iterate_timer = time.time()
        self._job_handler = job_handler
        self._job_cache = job_cache
        self._jobs = dict()
    def clear(self):
        db = self._get_db()
        db.remove(dict(
            compute_resource_id=self._compute_resource_id
        ))
    def run(self):
        while True:
            self._iterate()
            time.sleep(0.02)
    def _iterate(self):
        elapsed = time.time() - self._iterate_timer
        if elapsed < 3:
            return

        self._report_active()
        active_job_handler_ids = self._get_active_job_handler_ids()

        self._iterate_timer = time.time()
        db = self._get_db()

        # Handle pending jobs
        query = dict(
            compute_resource_id=self._compute_resource_id,
            last_modified_by_compute_resource=False,
            status='queued',
            compute_resource_status='pending'
        )
        for doc in db.find(query):
            self._handle_pending_job(doc)
        
        # Handle jobs
        job_ids = list(self._jobs.keys())
        for job_id in job_ids:
            job = self._jobs[job_id]
            reported_status = getattr(job, '_reported_status')
            if job._status == 'running':
                if reported_status != 'running':
                    print(f'Job running: {job_id}')
                    filter0 = dict(
                        compute_resource_id=self._compute_resource_id,
                        job_id=job_id
                    )
                    update = {
                        '$set': dict(
                            status='running',
                            compute_resource_status='running',
                            last_modified_by_compute_resource=True
                        )
                    }
                    db.update_one(filter0, update=update)
                    setattr(job, '_reported_status', 'running')
            elif job._status == 'finished':
                print(f'Job finished: {job_id}')
                if job._download_results:
                    _upload_files_as_needed_in_item(job._result, kachery=self._kachery)
                self._mark_job_as_finished(job_id=job._job_id, runtime_info=job._runtime_info, result=job._result)
                if self._job_cache is not None:
                    self._job_cache.cache_job_result(job)
                del self._jobs[job_id]
            elif job._status == 'error':
                print(f'Job error: {job_id}')
                self._mark_job_as_error(job_id=job_id, runtime_info=job._runtime_info, exception='test-exception')
                del self._jobs[job_id]
            
            # check if handler is still active
            if job_id in self._jobs:
                handler_id = getattr(job, '_handler_id')
                if handler_id not in active_job_handler_ids:
                    print(f'Removing job because client handler is no longer active: {job_id}')
                    self._job_handler.cancel_job(job_id)
                    filter0 = dict(
                        compute_resource_id=self._compute_resource_id,
                        job_id=job_id
                    )
                    db.remove(filter0)
                    del self._jobs[job_id]
        
        self._job_handler.iterate()
    
    def _get_active_job_handler_ids(self):
        db = self._get_db(collection='active_job_handlers')
        db_jobs = self._get_db()

        # remove the expired job handlers
        t0 = _utctime() - 10
        query = dict(
            utctime={'$lt': t0}
        )
        for doc in db.find(query):
            handler_id = doc['handler_id']
            print(f'Removing job handler: {handler_id}')
            db.remove(dict(handler_id=handler_id))
            db_jobs.remove(dict(handler_id=handler_id))

        # return handler ids for those that were not removed
        return [doc['handler_id'] for doc in db.find({})]
    
    def _handle_pending_job(self, doc):
        job_id = doc["job_id"]
        label = doc['job_serialized']['label']
        print(f'Queuing job: {label}')
        
        try:
            job_serialized = doc['job_serialized']
            job_serialized['code'] = ka.load_object(job_serialized['code'], fr=self._kachery)
            container = job_serialized['container']
            if container is None:
                raise Exception('Cannot run serialized job outside of container.')
            _prepare_container(container)
        except Exception as e:
            print(f'Error handing pending job: {label}')
            print(e)
            self._mark_job_as_error(job_id=job_id, exception=e, runtime_info=None)
            return
        
        job = _deserialize_job(job_serialized)
        if self._job_cache:
            self._job_cache.check_job(job)
        db = self._get_db()
        filter0 = dict(
            compute_resource_id=self._compute_resource_id,
            job_id=doc['job_id']
        )
        if job._status == 'finished':
            print(f'Found job in cache: {label}')
            self._mark_job_as_finished(job_id=job_id, result=job._result, runtime_info=job._runtime_info)
        elif job._status == 'error':
            print(f'Found error job in cache: {label}')
            self._mark_job_as_error(job_id=job_id, exception=job._exception, runtime_info=job._runtime_info)
        else:
            try:
                _download_files_as_needed_in_item(job._kwargs, kachery=self._kachery)
            except Exception as e:
                print(f'Error downloading input files for job: {label}')
                print(e)
                self._mark_job_as_error(job_id=job_id, exception=e, runtime_info=None)
                return
            self._jobs[job_id] = job
            self._job_handler.handle_job(job)
            update = {
                '$set': dict(
                    compute_resource_status='queued',
                    last_modified_by_compute_resource=True
                )
            }
            db.update_one(filter0, update=update)
            setattr(job, '_reported_status', 'queued')
            setattr(job, '_handler_id', doc['handler_id'])
    
    def _mark_job_as_error(self, *, job_id, runtime_info, exception):
        print(f'Job error: {job_id}')
        db = self._get_db()
        filter0 = dict(
            compute_resource_id=self._compute_resource_id,
            job_id=job_id
        )
        update = {
            '$set': dict(
                status='error',
                compute_resource_status='error',
                result=None,
                runtime_info=runtime_info,
                exception='{}'.format(exception),
                last_modified_by_compute_resource=True
            )
        }
        db.update_one(filter0, update=update)
    
    def _mark_job_as_finished(self, *, job_id, runtime_info, result):
        db = self._get_db()
        filter0 = dict(
            compute_resource_id=self._compute_resource_id,
            job_id=job_id
        )
        update = {
            '$set': dict(
                status='finished',
                compute_resource_status='finished',
                result=_serialize_item(result),
                runtime_info=runtime_info,
                exception=None,
                last_modified_by_compute_resource=True
            )
        }
        db.update_one(filter0, update=update)
    
    def _report_active(self):
        db = self._get_db(collection='active_compute_resources')
        filter = dict(
            compute_resource_id=self._compute_resource_id
        )
        update = {
            '$set': dict(
                compute_resource_id=self._compute_resource_id,
                kachery=self._kachery,
                utctime=_utctime()
            )
        }
        db.update_one(filter, update=update, upsert=True)

    def _get_db(self, collection='hither2_jobs'):
        return self._database.collection(collection)

def _download_files_as_needed_in_item(x, *, kachery):
    if isinstance(x, File):
        p = ka.load_file(x._sha1_path, fr=kachery)
        if p is None:
            raise Exception(f'Unable to download file: {x._sha1_path}')
    elif type(x) == dict:
        for val in x.values():
            _download_files_as_needed_in_item(val, kachery=kachery)
    elif type(x) == list:
        for val in x:
            _download_files_as_needed_in_item(val, kachery=kachery)
    elif type(x) == tuple:
        for val in x:
            _download_files_as_needed_in_item(val, kachery=kachery)
    else:
        pass

def _upload_files_as_needed_in_item(x, *, kachery):
    if isinstance(x, File):
        if kachery is not None:
            ka.store_file(x._sha1_path, to=kachery)
    elif type(x) == dict:
        for val in x.values():
            _upload_files_as_needed_in_item(val, kachery=kachery)
    elif type(x) == list:
        for val in x:
            _upload_files_as_needed_in_item(val, kachery=kachery)
    elif type(x) == tuple:
        for val in x:
            _upload_files_as_needed_in_item(val, kachery=kachery)
    else:
        pass

def _utctime():
    from datetime import datetime, timezone
    return datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()