import ast
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, request, jsonify, redirect, url_for
from sqlalchemy import create_engine
from community_stats import initialize_counter, resume_total, record_resume_processed


class CommunityCountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.tmp.name + '/counter.db')
        initialize_counter(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_concurrent_increments_and_restart(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _: record_resume_processed(self.engine), range(40)))
        initialize_counter(self.engine)
        self.assertEqual(resume_total(self.engine), 5274)
        self.engine.dispose()
        self.assertEqual(resume_total(self.engine), 5274)

    def test_routes_only_count_successful_processing(self):
        import os, re, uuid, logging
        from werkzeug.utils import secure_filename
        app = Flask(__name__)
        app.config['UPLOAD_FOLDER'] = self.tmp.name
        noop = lambda *a, **kw: lambda f: f
        ns = dict(app=app, db=SimpleNamespace(engine=self.engine),
                  request=request, jsonify=jsonify, redirect=redirect, url_for=url_for,
                  current_user=SimpleNamespace(is_authenticated=True),
                  limiter=SimpleNamespace(limit=noop), login_required=lambda f:f,
                  logger=logging.getLogger('test'), io=io, os=os, re=re, uuid=uuid,
                  secure_filename=secure_filename, resume_total=resume_total,
                  record_resume_processed=record_resume_processed,
                  _RESUME_MATCH_CACHE={}, _RESUME_MATCH_CACHE_MAX=200,
                  extract_text_from_file=lambda *a:'resume',
                  compare_resume_to_job=Mock(return_value='Score 85'),
                  generate_summary=Mock(return_value='Summary'),
                  render_template=lambda *a,**kw:'page',
                  generate_basic_analysis=lambda *a:{},
                  RateLimitError=type('RateLimitError',(Exception,),{}),
                  AuthenticationError=type('AuthenticationError',(Exception,),{}))
        names={'community_resume_count','_record_community_resume','resumemat','upload_resume'}
        tree=ast.parse(Path('app.py').read_text())
        selected=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
        exec(compile(selected,'app.py','exec'),ns)
        client=app.test_client()
        self.assertEqual(client.get('/api/community-resume-count').json['total'],5234)
        self.assertEqual(client.post('/api/community-resume-count').status_code,405)
        client.get('/resume-jobmatch')
        self.assertEqual(resume_total(self.engine),5234)
        old=os.getcwd()
        try:
            os.chdir(self.tmp.name)
            r=client.post('/resume-jobmatch',data={'job_req':'job','resume':(io.BytesIO(b'resume'),'r.txt')})
            client.get(r.location)
            client.get(r.location)
            self.assertEqual(resume_total(self.engine),5235)
            r=client.post('/uploads',data={'file':(io.BytesIO(b'resume'),'r.txt')})
            self.assertEqual(r.json['status'],'success')
            self.assertEqual(resume_total(self.engine),5236)
            ns['generate_summary'].side_effect=RuntimeError('AI unavailable')
            client.post('/uploads',data={'file':(io.BytesIO(b'resume'),'r.txt')})
            client.post('/uploads',data={'file':(io.BytesIO(b''),'r.txt')})
            ns['compare_resume_to_job'].side_effect=RuntimeError('AI unavailable')
            client.post('/resume-jobmatch',data={'job_req':'job','resume':(io.BytesIO(b'resume'),'r.txt')})
            self.assertEqual(resume_total(self.engine),5236)
        finally:
            os.chdir(old)

if __name__=='__main__': unittest.main()
