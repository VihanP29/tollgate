"""Side-effect-free replay against frozen historical inputs."""
import copy
import uuid
from tollgate.semantic_policy import compose,evaluate,validate_policy

def run_replay(gateway,session,body,policy_get):
 if not session.policy: raise ValueError('Create a showcase mandate before replay')
 mode=body.get('mode','threshold')
 if mode not in ('threshold','policy'): raise ValueError('Unknown replay mode')
 target=copy.deepcopy(policy_get(body.get('policy_version_id') or session.policy['id']))
 if mode=='threshold':
  target['review_at']=body.get('review_at',target['review_at']);target['deny_above']=body.get('deny_above',target['deny_above'])
  for rule in target.get('rules',{}).values(): rule.update(review_at=target['review_at'],deny_above=target['deny_above'])
  target=validate_policy(target)
 rows=[]
 events=[e for e in session.events if e.semantic_evaluation]
 if not events: raise ValueError('Evaluate a showcase payment before replay')
 if len(events)>30: raise ValueError('Replay is limited to 30 recorded evaluations')
 for event in events:
  original=event.semantic_evaluation
  if mode=='threshold':
   if original['policy']['questions']!=target['questions'] or original['policy']['model']!=target['model'] or original['policy'].get('shared_guidance','')!=target.get('shared_guidance','') or original['policy'].get('compiler_version')!=target.get('compiler_version'): raise ValueError('Changed questions or model require fresh policy replay')
   response=copy.deepcopy(original);checks,queue=compose(response['answers'],target)
  else: checks,response=evaluate(gateway.reviewer.client,original['state'],target,gateway.reviewer.backend=='offline');queue=response['queue']
  hard=[c.model_dump() for c in event.checks if c.kind!='semantic']
  combined=hard+checks
  verdict='deny' if any(c['outcome']=='fail' for c in combined) else 'escalate' if any(c['outcome'] in ('review','unavailable') for c in combined) else 'allow'
  rows.append(dict(event_id=event.event_id,invoice_id=event.args['memo'],original=event.verdict,replay=verdict,queue=queue,evaluation=dict(response,policy=target)))
 return dict(id='RPL-'+uuid.uuid4().hex[:12],mode=mode,side_effect_free=True,model_calls=len(rows) if mode=='policy' and gateway.reviewer.backend!='offline' else 0,policy=target,rows=rows)
