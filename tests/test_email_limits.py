import ast
import hashlib
import unittest
from pathlib import Path
from flask import Flask, Blueprint, request
from flask_limiter import Limiter

class EmailLimitsTest(unittest.TestCase):
    def test_recipient_limit_across_ips_and_both_forms(self):
        app=Flask(__name__)
        limiter=Limiter(lambda: request.headers.get('X-Test-IP','one'),app=app,storage_uri='memory://')
        auth=Blueprint('auth',__name__)
        tree=ast.parse(Path('website/auth.py').read_text())
        functions=[]
        for n in tree.body:
            if isinstance(n,ast.FunctionDef) and n.name in {'_email_rate_key','resend_verification','forgot_password'}:
                if n.name!='_email_rate_key':n.body=[ast.Return(value=ast.Constant(value='sent'))]
                functions.append(n)
        ns=dict(auth=auth,limiter=limiter,request=request,hashlib=hashlib)
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),'test','exec'),ns)
        app.register_blueprint(auth)
        client=app.test_client()
        for i in range(3):
            path='/resend-verification' if i%2 else '/forgot-password'
            self.assertEqual(client.post(path,data={'email':' Person@Example.com '},headers={'X-Test-IP':str(i)}).status_code,200)
        self.assertEqual(client.post('/resend-verification',data={'email':'person@example.com'},headers={'X-Test-IP':'new'}).status_code,429)
        self.assertEqual(client.get('/forgot-password').status_code,200)
        self.assertEqual(client.post('/resend-verification',data={'email':'different@example.com'},headers={'X-Test-IP':'new'}).status_code,200)
