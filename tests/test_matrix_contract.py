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
        # The original successful job ID remains authenticated, not just its name.
        value['outcomes'][0]['job_id']=99
        value['outcomes'][0]['job_url']=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/99'
        with self.assertRaisesRegex(ValueError,'effective API job identities'):
            m.verify_jobs(value,run,jobs,'a'*40)

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

    def test_historical_producer_must_be_terminal(self):
        value,run,jobs=self.fixture()
        run.update(status='in_progress',conclusion=None)
        with self.assertRaises(m.PendingJobs):m.verify_jobs(value,run,jobs,'a'*40)

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
        run.update(status='completed',conclusion='cancelled')
        with self.assertRaises(ValueError):m.verify_jobs(value,run,jobs,'a'*40)


class InheritedJobCopyTests(unittest.TestCase):
    def fixture(self):
        value,run,jobs=MatrixContractTests().package_fixture()
        original=jobs[3]
        # GitHub run #524 returned this shape: a new attempt/ID created the
        # next day, preserving the complete execution metadata from attempt 1.
        original.update(id=114777546673,run_attempt=1,created_at='2026-10-12T10:54:44Z',
                        started_at='2026-10-12T10:54:46Z',completed_at='2026-10-12T10:55:58Z',
                        runner_id=1000013085,runner_name='GitHub Actions 1000013085',
                        runner_group_id=0,runner_group_name='GitHub Actions',labels=['ubuntu-latest'],
                        steps=[{'name':'Package','number':1,'status':'completed','conclusion':'success',
                                'started_at':'2026-10-12T10:54:46Z','completed_at':'2026-10-12T10:55:58Z'}],
                        html_url=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/114777546673')
        inherited=copy.deepcopy(original)
        inherited.update(id=115090810557,run_attempt=2,created_at='2026-10-13T01:53:43Z',runner_group_id=None,
                         html_url=f'https://github.com/{m.REPOSITORY}/actions/runs/10/job/115090810557')
        jobs.append(inherited)
        value['outcomes'][0].update(job_id=original['id'],job_url=original['html_url'])
        results={arch:dict(schema_version=1,version=value['version'],architecture=arch,producer_run_id=10,
                          producer_run_attempt=1 if arch=='amd64' else 2,producer_head_sha='a'*40,
                          build_repository_commit='a'*40,generation='10-44',
                          status='success' if arch=='amd64' else 'failure',
                          reason='' if arch=='amd64' else 'Package step failure')
                 for arch in value['requested_architectures']}
        return value,run,jobs,results,original,inherited

    def test_copied_success_keeps_record_and_historical_producer_bound_to_original_job(self):
        for group in (None,0):
            value,run,jobs,results,original,inherited=self.fixture()
            inherited['runner_group_id']=group
            effective=m.effective_jobs(jobs,10,2,'a'*40)
            self.assertIs(next(job for job in effective if job['name']=='compile (amd64)'),original)
            outcomes=m.collect_recorded(jobs,results,value['requested_architectures'],10,2,
                                        'a'*40,'a'*40,'10-44',value['version'])
            self.assertEqual(outcomes,value['outcomes'])
            self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64'])
        # Historical manifests bind native job failures rather than package
        # step reports; their original successful job ID must also survive.
        value,run,jobs,_,_,_=self.fixture();value.pop('outcome_source')
        jobs[4]['conclusion']='failure';value['outcomes'][1]['reason']='GitHub job failure'
        jobs=[job for job in jobs if job['name']!='matrix_status']
        self.assertEqual(m.verify_jobs(value,run,jobs,'a'*40),['amd64'])

    def test_genuine_new_executions_are_decisive_even_when_old_success_exists(self):
        for conclusion in ('success','failure','timed_out','cancelled','skipped',None):
            value,run,jobs,results,original,newest=self.fixture()
            newest.update(created_at='2026-10-13T01:53:43Z',started_at='2026-10-13T01:53:45Z',
                          completed_at='2026-10-13T01:54:57Z' if conclusion else None,
                          status='completed' if conclusion else 'in_progress',conclusion=conclusion)
            with self.subTest(conclusion=conclusion):
                effective=m.effective_jobs(jobs,10,2,'a'*40)
                self.assertIs(next(job for job in effective if job['name']=='compile (amd64)'),newest)
                with self.assertRaisesRegex(ValueError,'older execution: amd64'):
                    m.collect_recorded(jobs,results,value['requested_architectures'],10,2,
                                       'a'*40,'a'*40,'10-44',value['version'])
                with self.assertRaises(ValueError):m.verify_jobs(value,run,jobs,'a'*40)

    def test_copied_metadata_must_authenticate_one_exact_original_execution(self):
        for fault in ('steps','runner','group','labels','started','failed','missing_field','missing_original',
                      'bad_original_created','unzoned','wrong_run','wrong_head','duplicate_id','duplicate_attempt'):
            value,run,jobs,_,original,inherited=self.fixture()
            if fault=='steps':inherited['steps'][0]['name']='Different execution'
            elif fault=='runner':inherited['runner_id']+=1
            elif fault=='group':inherited['runner_group_id']=7
            elif fault=='labels':inherited['labels']=['self-hosted']
            elif fault=='started':inherited['started_at']='2026-10-12T10:54:47Z'
            elif fault=='failed':inherited['conclusion']='failure'
            elif fault=='missing_field':inherited.pop('steps')
            elif fault=='missing_original':jobs.remove(original)
            elif fault=='bad_original_created':original['created_at']='2026-10-12T10:55:00Z'
            elif fault=='unzoned':inherited['created_at']='2026-10-13T01:53:43'
            elif fault=='wrong_run':inherited['run_id']=11
            elif fault=='wrong_head':inherited['head_sha']='b'*40
            elif fault=='duplicate_id':inherited['id']=original['id']
            elif fault=='duplicate_attempt':inherited['run_attempt']=1
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                m.effective_jobs(jobs,10,2,'a'*40)

    def test_ambiguous_originals_and_copies_without_timing_evidence_never_inherit(self):
        _,_,jobs,_,original,inherited=self.fixture()
        second=copy.deepcopy(original)
        second.update(id=99,run_attempt=2)
        inherited['run_attempt']=3
        with self.assertRaisesRegex(ValueError,'ambiguous original'):
            m.effective_jobs(jobs+[second],10,3,'a'*40)
        for missing in ('created_at','completed_at'):
            value,_,jobs,results,_,inherited=self.fixture();inherited.pop(missing)
            with self.subTest(missing=missing):
                self.assertEqual(next(job for job in m.effective_jobs(jobs,10,2,'a'*40)
                                      if job['name']=='compile (amd64)')['id'],inherited['id'])
                with self.assertRaisesRegex(ValueError,'older execution: amd64'):
                    m.collect_recorded(jobs,results,value['requested_architectures'],10,2,
                                       'a'*40,'a'*40,'10-44',value['version'])

    def test_skipped_nodes_with_late_creation_remain_skipped_and_future_copies_are_ignored(self):
        value,run,jobs,_,original,inherited=self.fixture()
        skipped=dict(jobs[0],id=77,name='candidate',status='completed',conclusion='skipped',steps=[],
                     created_at='2026-10-22T13:36:44Z',completed_at='2026-10-22T13:36:34Z')
        self.assertEqual(m.verify_jobs(value,run,jobs+[skipped],'a'*40),['amd64'])
        future=dict(inherited,id=78,run_attempt=3,head_sha='b'*40)
        self.assertEqual(next(job for job in m.effective_jobs(jobs+[future],10,2,'a'*40)
                              if job['name']=='compile (amd64)')['id'],original['id'])
        skipped.update(name='compile (amd64)',run_attempt=3)
        self.assertIs(next(job for job in m.effective_jobs(jobs+[skipped],10,3,'a'*40)
                           if job['name']=='compile (amd64)'),skipped)


class MergedPRMatrixTests(unittest.TestCase):
    def fixture(self):
        value,run,jobs=MatrixContractTests().fixture()
        # CI #654 retained its valid synthetic checkout after the merged PR
        # disappeared from both run and attempt API pull_requests collections.
        head='eab95e16be4909bce87112aa6ee762a585a1a66c'
        base='d34fad401e225855a45ba92b42bce57ae026ad2d'
        executed='cff245117a6a7b09226556ec546a6d2b548e80bd'
        value['producer_head_sha']=head
        run.update(event='pull_request',head_sha=head,pull_requests=[])
        for job in jobs:job['head_sha']=head
        build=next(job for job in jobs if job['name']=='build')
        build['steps']=[{'name':'Run actions/checkout@v4','number':3,'status':'completed',
                         'conclusion':'success','started_at':'2026-10-22T14:50:45Z',
                         'completed_at':'2026-10-22T14:50:47Z'}]
        bodies=[
            '##[group]Run actions/checkout@v4',
            'Syncing repository: '+m.REPOSITORY,
            '[command]/usr/bin/git -c protocol.version=2 fetch --no-tags --prune '
            '--no-recurse-submodules --depth=1 origin +'+executed+':refs/remotes/pull/28/merge',
            '[command]/usr/bin/git checkout --progress --force refs/remotes/pull/28/merge',
            'HEAD is now at '+executed[:7]+' Merge '+head+' into '+base,
            '[command]/usr/bin/git log -1 --format=%H',
            executed,
        ]
        lines=['2026-10-22T14:50:47.%07dZ %s'%(index,body) for index,body in enumerate(bodies)]
        pr={'number':28,'state':'closed','merged':True,
            'head':{'sha':head,'repo':{'full_name':m.REPOSITORY}},
            'base':{'sha':base,'repo':{'full_name':m.REPOSITORY}}}
        commit={'sha':executed,'tree':{'sha':'d'*40},'parents':[{'sha':base},{'sha':head}]}
        return value,run,jobs,commit,pr,lines

    def verify(self, fixture, fetch_json=None, fetch_log=None):
        value,run,jobs,commit,pr,lines=fixture
        return m.verify_jobs(value,run,jobs,commit['sha'],commit,
                             fetch_json=fetch_json or (lambda path:pr),
                             fetch_log=fetch_log or (lambda job_id:'\n'.join(lines)+'\n'))

    def test_empty_run_pr_link_requires_authenticated_successful_checkout(self):
        fixture=self.fixture()
        api=unittest.mock.Mock(return_value=fixture[4])
        logs=unittest.mock.Mock(return_value='\n'.join(fixture[5])+'\n')
        self.assertEqual(self.verify(fixture,api,logs),['amd64'])
        api.assert_called_once_with(f'repos/{m.REPOSITORY}/pulls/28')
        logs.assert_called_once_with(3)
        with self.assertRaisesRegex(ValueError,'authenticated merged PR checkout'):
            m.verify_jobs(fixture[0],fixture[1],fixture[2],fixture[3]['sha'],fixture[3])
        for status,conclusion in [('in_progress',None),('completed','failure')]:
            changed=self.fixture();changed[2][2].update(status=status,conclusion=conclusion)
            with self.subTest(status=status),self.assertRaises(ValueError):
                self.verify(changed,api,logs)
        self.assertEqual(logs.call_count,1)

    def test_live_pr_binding_does_not_use_merged_fallback(self):
        fixture=self.fixture();pr=fixture[4]
        fixture[1]['pull_requests']=[{'base':{'sha':pr['base']['sha']},'head':{'sha':pr['head']['sha']}}]
        api=unittest.mock.Mock(side_effect=AssertionError('No merged fallback for a live binding'))
        logs=unittest.mock.Mock(side_effect=AssertionError('No checkout log required'))
        self.assertEqual(self.verify(fixture,api,logs),['amd64'])
        api.assert_not_called();logs.assert_not_called()
        for prs in (None,{},'', [{},{}], [None], [{'base':None,'head':None}]):
            fixture=self.fixture();fixture[1]['pull_requests']=prs
            with self.subTest(prs=prs),self.assertRaises(ValueError):self.verify(fixture,api,logs)
        api.assert_not_called();logs.assert_not_called()

    def test_merged_pr_and_merge_object_must_match_frozen_checkout(self):
        for kind in ('number','open','not_merged','bool','head','base','head_repo','base_repo','parents','tree'):
            fixture=self.fixture();pr,commit=fixture[4],fixture[3]
            if kind=='number':pr['number']=29
            elif kind=='open':pr['state']='open'
            elif kind=='not_merged':pr['merged']=False
            elif kind=='bool':pr['merged']=1
            elif kind in ('head','base'):pr[kind]['sha']='f'*40
            elif kind.endswith('_repo'):pr[kind[:-5]]['repo']['full_name']='other/repository'
            elif kind=='parents':commit['parents'].reverse()
            elif kind=='tree':commit['tree']['sha']='latest'
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.verify(fixture)

    def test_checkout_markers_cannot_be_spoofed_duplicated_or_moved_to_a_later_step(self):
        for kind in ('ref','fetch_sha','short_sha','head','repo','duplicate','later_step','out_of_step','missing'):
            fixture=self.fixture();lines=fixture[5];executed=fixture[3]['sha']
            if kind=='ref':lines[3]=lines[3].replace('/28/','/29/')
            elif kind=='fetch_sha':lines[2]=lines[2].replace(executed,'f'*40)
            elif kind=='short_sha':lines[6]=lines[6].replace(executed,executed[:7])
            elif kind=='head':lines[4]=lines[4].replace(fixture[1]['head_sha'],'f'*40)
            elif kind=='repo':lines[1]=lines[1].replace(m.REPOSITORY,'other/repository')
            elif kind=='duplicate':lines.append(lines[6])
            elif kind=='later_step':lines.insert(6,'2026-10-22T14:50:47.0000005Z ##[group]Run arbitrary-step')
            elif kind=='out_of_step':lines[6]=lines[6].replace('14:50:47.','14:50:49.')
            elif kind=='missing':lines.pop(5)
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.verify(fixture)
        fixture=self.fixture();executed=fixture[3]['sha']
        fixture[5][6]=fixture[5][6].replace(executed,'\x1b[32m'+executed+'\x1b[0m')
        self.assertEqual(self.verify(fixture),['amd64'])
        fixture=self.fixture();executed=fixture[3]['sha']
        fixture[5][6]=fixture[5][6].replace(executed,executed[:20]+'\x1b[32m'+executed[20:])
        with self.assertRaises(ValueError):self.verify(fixture)

    def test_checkout_metadata_and_parser_budget_are_required(self):
        for kind in ('missing','duplicate','failed','number','invalid_time','interval','budget'):
            fixture=self.fixture();build=fixture[2][2]
            if kind=='missing':build.pop('steps')
            elif kind=='duplicate':build['steps'].append(copy.deepcopy(build['steps'][0]))
            elif kind=='failed':build['steps'][0]['conclusion']='failure'
            elif kind=='number':build['steps'][0]['number']=0
            elif kind=='invalid_time':build['steps'][0]['started_at']='invalid'
            elif kind=='interval':build['steps'][0]['started_at']='2026-10-22T14:50:49Z'
            elif kind=='budget':fixture[5].append('x'*(8*1024*1024))
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.verify(fixture)
