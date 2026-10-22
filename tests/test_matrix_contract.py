import copy
import sys
import unittest
from unittest.mock import patch
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
            if mutation=='attempt':jobs[-1]['run_attempt']=3
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

    def test_failed_only_retry_uses_inherited_success_and_latest_branch(self):
        value,run,jobs=self.fixture()
        for job in jobs:
            if job['name'] in ('tests','prepare','compile (amd64)'):job['run_attempt']=1
        previous=dict(jobs[-1],id=50,run_attempt=1)
        jobs[-1]['conclusion']='success'
        old_writer=dict(jobs[0],id=51,name='automatic_publication',conclusion='failure')
        future=dict(jobs[-1],id=52,run_attempt=3,conclusion='failure')
        jobs.extend([previous,old_writer,future])
        value['outcomes']=m.collect(jobs,value['requested_architectures'],10,2)
        value['artifacts'].append({'architecture':'arm64'})
        run['conclusion']='success'
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64','arm64'])
        run.update(status='in_progress',conclusion=None)
        jobs.append(dict(jobs[0],id=53,name='publication_prepare',run_attempt=2,
                         status='in_progress',conclusion=None))
        self.assertEqual(m.verify_current_jobs(value,run,jobs,'a'*40,10,2),['amd64','arm64'])
        # The original successful job ID remains authenticated, not just its name.
        value['outcomes'][0]['job_id']=99
        value['outcomes'][0]['job_url']=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/99'
        with self.assertRaisesRegex(ValueError,'effective API job identities'):
            m.verify_current_jobs(value,run,jobs,'a'*40,10,2)

    def test_manifest_cannot_hide_a_successful_architecture(self):
        value,run,jobs=self.fixture()
        jobs[-1]['conclusion']='success';run['conclusion']='success'
        value['requested_architectures']=['amd64']
        value['outcomes']=value['outcomes'][:1]
        with self.assertRaisesRegex(ValueError,'Unrequested architecture jobs'):
            m.verify_jobs(value,run,jobs,'a'*40)

    def test_only_incomplete_snapshots_are_retried(self):
        _,_,jobs=self.fixture()
        pending=copy.deepcopy(jobs);pending[-1].update(status='in_progress',conclusion=None)
        snapshots=iter([pending,jobs])
        with patch.object(m.time,'sleep') as sleep:
            result=m.retry_pending(lambda:m.collect(next(snapshots),['amd64','arm64'],10,2))
        self.assertEqual([r['status'] for r in result],['success','failure'])
        sleep.assert_called_once()
        for kind in ('cancelled','skipped','wrong_head','unknown_state','contradictory'):
            bad=copy.deepcopy(jobs)
            if kind=='wrong_head':bad[-1]['head_sha']='b'*40
            elif kind=='unknown_state':bad[-1].update(status='unknown-state',conclusion=None)
            elif kind=='contradictory':bad[-1].update(status='in_progress',conclusion='success')
            else:bad[-1]['conclusion']=kind
            with self.subTest(kind=kind),patch.object(m.time,'sleep') as sleep:
                with self.assertRaises(ValueError):
                    m.retry_pending(lambda:m.collect(bad,['amd64','arm64'],10,2,head_sha='a'*40))
                sleep.assert_not_called()
        with patch.object(m.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,r'did not settle.*arm64: job=5.*in_progress'):
                m.retry_pending(lambda:m.collect(pending,['amd64','arm64'],10,2),attempts=3)
            self.assertEqual(sleep.call_count,2)

    def test_live_and_historical_producer_states_keep_strict_boundaries(self):
        value,run,jobs=self.fixture()
        run.update(status='in_progress',conclusion=None)
        with self.assertRaises(m.PendingJobs):m.verify_jobs(value,run,jobs,'a'*40)
        publication=dict(jobs[0],id=6,name='publication_prepare',status='queued',conclusion=None)
        self.assertEqual(m.verify_current_jobs(value,run,jobs+[publication],'a'*40,10,2),['amd64'])
        publication['status']='unknown-state'
        with self.assertRaises(ValueError) as error:
            m.verify_current_jobs(value,run,jobs+[publication],'a'*40,10,2)
        self.assertNotIsInstance(error.exception,m.PendingJobs)

    def test_job_pagination_does_not_accept_a_truncated_inventory(self):
        api=unittest.mock.Mock(side_effect=[{'jobs':[{'id':1}],'total_count':2},
                                           {'jobs':[],'total_count':2}])
        with self.assertRaises(m.PendingJobs):m.read_jobs(api,m.REPOSITORY,10)
        self.assertEqual(api.call_count,2)

    def package_fixture(self, partial=True):
        value,run,jobs=self.fixture()
        value['outcome_source']='package-step'
        for job in jobs:
            job['conclusion']='success'
        if partial:
            value['outcomes'][1]['reason']='Package step failure'
        else:
            value['outcomes'][1].update(status='success',reason='')
            value['artifacts'].append({'architecture':'arm64'})
        run['conclusion']='failure' if partial else 'success'
        jobs.append(dict(jobs[0],id=6,name='matrix_status',conclusion=run['conclusion'],
                         html_url=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/6'))
        return value,run,jobs

    def test_package_outcomes_preserve_partial_failure_without_native_job_failure(self):
        value,run,jobs=self.package_fixture()
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64'])
        # A post-job or upload failure must not disappear behind another
        # architecture's controlled package failure.
        for conclusion in ('failure','timed_out','cancelled','skipped'):
            changed=copy.deepcopy(jobs)
            changed[3]['conclusion']=conclusion
            with self.subTest(conclusion=conclusion),self.assertRaises(ValueError):
                m.verify_jobs(value,run,changed,'a'*40)

    def test_package_mode_requires_its_own_exact_terminal_matrix_status(self):
        for partial in (False,True):
            for state in ('missing','previous-attempt','pending','wrong-result','skipped','unknown'):
                value,run,jobs=self.package_fixture(partial)
                if state=='missing':jobs.pop()
                elif state=='previous-attempt':jobs[-1]['run_attempt']=1
                elif state=='pending':jobs[-1].update(status='in_progress',conclusion=None)
                elif state=='wrong-result':jobs[-1]['conclusion']='success' if partial else 'failure'
                elif state=='skipped':jobs[-1]['conclusion']='skipped'
                elif state=='unknown':jobs[-1].update(status='unknown-state',conclusion=None)
                with self.subTest(partial=partial,state=state),self.assertRaises(ValueError):
                    m.verify_jobs(value,run,jobs,'a'*40)
        value,run,jobs=self.package_fixture(False)
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64','arm64'])

    def test_package_mode_authenticates_inherited_and_latest_branch_identities(self):
        value,run,jobs=self.package_fixture()
        jobs[3]['run_attempt']=1
        previous=dict(jobs[4],id=50,run_attempt=1,
                      html_url=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/50')
        future=dict(jobs[4],id=51,run_attempt=3,conclusion='failure')
        jobs.extend([previous,future])
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64'])
        value['outcomes'][1].update(job_id=50,job_url=previous['html_url'])
        with self.assertRaisesRegex(ValueError,'effective API job identities'):
            m.verify_jobs(value,run,jobs,'a'*40)

    def test_package_mode_terminal_producer_explains_only_matrix_and_permitted_writer_failures(self):
        for partial in (False,True):
            value,run,jobs=self.package_fixture(partial)
            run['conclusion']='failure'
            writer=dict(jobs[0],id=7,name='automatic_publication',conclusion='failure')
            with self.subTest(partial=partial):
                with self.assertRaises(ValueError):
                    m.verify_jobs(value,run,jobs+[writer],'a'*40)
                self.assertEqual(m.verify_jobs(value,run,jobs+[writer],'a'*40,
                                              allow_publication_failures=True),
                                 ['amd64'] if partial else ['amd64','arm64'])
                hidden=dict(writer,id=8,name='unexpected')
                with self.assertRaises(ValueError):
                    m.verify_jobs(value,run,jobs+[writer,hidden],'a'*40,
                                  allow_publication_failures=True)
        value,run,jobs=self.package_fixture()
        run.update(status='in_progress',conclusion=None)
        with self.assertRaises(m.PendingJobs):m.verify_jobs(value,run,jobs,'a'*40)
        with self.assertRaises(m.PendingJobs):m.verify_current_jobs(value,run,jobs,'a'*40,10,2)
        run.update(status='completed',conclusion='cancelled')
        with self.assertRaises(ValueError):m.verify_jobs(value,run,jobs,'a'*40)
