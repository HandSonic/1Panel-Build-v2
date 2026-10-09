import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import matrix_contract as m

class MatrixContractTests(unittest.TestCase):
    def fixture(self):
        jobs=[]
        for i, name in enumerate(['tests','prepare','build','compile (amd64)','compile (arm64)'],1):
            jobs.append({'id':i,'name':name,'run_id':10,'run_attempt':2,'head_sha':'a'*40,'status':'completed',
                         'conclusion':'failure' if i==5 else 'success',
                         'html_url':f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/{i}'})
        value={'schema_version':2,'version':'v2.99.0','requested_architectures':['amd64','arm64'],
               'producer_run_id':10,'producer_run_attempt':2,'producer_head_sha':'a'*40,
               'outcomes':m.collect(jobs,['amd64','arm64'],10,2),'artifacts':[{'architecture':'amd64'}]}
        run={'event':'workflow_dispatch','id':10,'run_attempt':2,'head_sha':'a'*40,'status':'completed','conclusion':'failure','path':'.github/workflows/build.yml'}
        return value,run,jobs
    def test_partial_success_has_exact_api_explanation(self):
        value,run,jobs=self.fixture()
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64'])
    def test_undeclared_or_extra_artifacts_rejected(self):
        for mutation in ('omit','extra','duplicate','url','id','attempt','reason'):
            value,_,_=self.fixture()
            if mutation=='omit':value['outcomes'].pop()
            if mutation=='extra':value['artifacts'].append({'architecture':'arm64'})
            if mutation=='duplicate':value['outcomes'][1]=copy.deepcopy(value['outcomes'][0])
            if mutation=='url':value['outcomes'][0]['job_url']='https://example.org/job/4'
            if mutation=='id':value['outcomes'][0]['job_id']=True
            if mutation=='attempt':value['producer_run_attempt']=0
            if mutation=='reason':value['outcomes'][1]['reason']=''
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):m.validate(value)
    def test_shared_failure_cancellation_and_attempt_drift_reject(self):
        for mutation in ('shared','cancel','attempt','hidden','wrongbranch','falsegreen'):
            value,run,jobs=self.fixture()
            if mutation=='shared':jobs[0]['conclusion']='failure'
            if mutation=='cancel':run['conclusion']='cancelled'
            if mutation=='attempt':jobs[-1]['run_attempt']=1
            if mutation=='hidden':jobs.append({'id':99,'name':'unexpected','conclusion':'failure'})
            if mutation=='wrongbranch':jobs[-1]['conclusion']='success'
            if mutation=='falsegreen':run['conclusion']='success'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):m.verify_jobs(value,run,jobs,'a'*40)
    def test_all_success_and_all_failed_are_explicit(self):
        value,run,jobs=self.fixture();jobs[-1]['conclusion']='success';run['conclusion']='success'
        value['outcomes']=m.collect(jobs,value['requested_architectures'],10,2)
        value['artifacts'].append({'architecture':'arm64'})
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64','arm64'])
        for job in jobs[3:]:job['conclusion']='failure'
        run['conclusion']='failure';value['artifacts']=[]
        value['outcomes']=m.collect(jobs,value['requested_architectures'],10,2)
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),[])

    def test_pr_execution_requires_recorded_base_and_head_parents(self):
        value,run,jobs=self.fixture();run['event']='pull_request'
        run['pull_requests']=[{'head':{'sha':'a'*40},'base':{'sha':'b'*40}}]
        commit={'sha':'c'*40,'tree':{'sha':'d'*40},'parents':[{'sha':'b'*40},{'sha':'a'*40}]}
        self.assertEqual(m.verify_jobs(value,run,jobs,'c'*40,commit),['amd64'])
        for kind in ('head','base','merge','tree','attempt','event'):
            bad_run=copy.deepcopy(run);bad_commit=copy.deepcopy(commit)
            if kind=='head':bad_commit['parents'][1]['sha']='e'*40
            if kind=='base':bad_commit['parents'][0]['sha']='e'*40
            if kind=='merge':bad_commit['sha']='e'*40
            if kind=='tree':bad_commit['tree']['sha']='latest'
            if kind=='attempt':bad_run['run_attempt']=1
            if kind=='event':bad_run['event']='pull_request_target'
            with self.subTest(kind=kind),self.assertRaises(ValueError):m.verify_jobs(value,bad_run,jobs,'c'*40,bad_commit)
        with self.assertRaises(ValueError):m.verify_jobs(value,run,jobs,'c'*40)

    def test_missing_inherited_job_is_not_silently_accepted(self):
        value,run,jobs=self.fixture()
        with self.assertRaisesRegex(ValueError,'Missing or duplicate architecture job'):
            m.verify_jobs(value,run,jobs[:-1],'a'*40)
