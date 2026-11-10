const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = (state, tone = '') => `<span class="badge ${tone}">${escapeHTML(state)}</span>`;
const stateBadge = state => badge(state, ['running','completed','alive'].includes(state) ? 'good' : ['failed','escalated','unreachable'].includes(state) ? 'bad' : 'warn');
const compute = seconds => ({type:'compute', seconds});
const presets = {
  parallel: {leader:{id:'leader', steps:[compute(15)]}, workers:[{id:'implement',steps:[compute(30),{type:'effect',key:'patches_applied',delta:1}]},{id:'test',steps:[compute(25)]},{id:'review',depends_on:['implement','test'],steps:[compute(15)]}]},
  recovery: {leader:{id:'leader',steps:[compute(10)]},workers:[{id:'implement',steps:[compute(10),{type:'effect',key:'patches_applied',delta:1},compute(15)]},{id:'review',depends_on:['implement'],steps:[compute(10)]}],faults:[{worker:'implement',attempt:1,step:1,kind:'effect_interruption'}]},
  simple: {leader:{id:'leader',steps:[compute(30)],result:'Task complete'}}
};
function loadPreset(){ $('spec').value=JSON.stringify(presets[$('preset').value],null,2); }
$('preset').addEventListener('change',loadPreset);loadPreset();
$('task-id').value=`workflow-${Date.now()}`;
async function api(path, body){
  const response=await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
  const result=await response.json();if(!response.ok)throw new Error(result.error || `Request failed (${response.status})`);return result;
}
let eventSequence = null;
function render(data){
  const occupied=data.vms.reduce((n,v)=>n+v.assignments.length,0), capacity=data.vms.reduce((n,v)=>n+v.capacity,0);
  const active=data.tasks.filter(t=>!['completed','failed','escalated'].includes(t.status));
  $('metrics').innerHTML=[['VMs online',`${data.vms.filter(v=>v.online).length} / ${data.vms.length}`],['Slots occupied',`${occupied} / ${capacity}`],['Active workloads',active.length],['Completed workloads',data.tasks.filter(t=>t.status==='completed').length]].map(([label,value])=>`<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join('');
  $('vms').innerHTML=data.vms.map(vm=>`<article class="vm"><div class="vm-heading"><h3>${escapeHTML(vm.id)}</h3>${badge(vm.online?'Online':'Offline',vm.online?'good':'bad')}</div><p class="hint">${vm.assignments.length} / ${vm.capacity} slots reserved · ${vm.last_heartbeat?`heartbeat ${Math.max(0,Math.floor(data.now-vm.last_heartbeat))}s ago`:'No heartbeat received'}</p>${Array.from({length:Math.max(vm.capacity,vm.assignments.length)},(_,i)=>{
    const w=vm.assignments[i];if(!w)return `<div class="slot empty"><div class="slot-head"><small>CAPACITY SLOT ${i+1}</small>${badge(vm.online?'Available':'Offline')}</div></div>`;
    const total=w.node.steps.length, step=w.node.steps[w.next_step];
    return `<div class="slot"><div class="slot-head"><small>CAPACITY SLOT ${i+1} · ${w.pid?`PID ${escapeHTML(w.pid)}`:'PID pending'}</small>${stateBadge(w.state)}</div><strong>${escapeHTML(w.id)}</strong><small>${escapeHTML(w.task_id)} · attempt ${w.attempt}</small><p>${stateBadge(w.process_state)} ${w.stalled?badge('Progress stalled','bad'):''}</p><p>${w.next_step} / ${total} steps complete${step?` · ${escapeHTML(step.type)} · ${w.progress} progress ticks`:''}</p><progress max="${total||1}" value="${w.next_step}" aria-label="Completed steps"></progress><p>Last progress ${Math.max(0,Math.floor(data.now-w.last_progress))}s ago${w.recovery_reason?` · ${escapeHTML(w.recovery_reason)}`:''}</p></div>`;
  }).join('')}</article>`).join('');
  $('task-count').textContent=`${data.tasks.length} total`;
  $('tasks').innerHTML=data.tasks.slice().reverse().map(t=>`<tr><td><strong>${escapeHTML(t.id)}</strong>${t.reason?`<p>${escapeHTML(t.reason)}</p>`:''}</td><td>${stateBadge(t.status)}</td><td>—</td><td>${Object.values(t.workers).filter(w=>w.state==='completed').length} / ${Object.keys(t.workers).length} workers</td></tr>${Object.values(t.workers).map(w=>`<tr class="worker"><td>↳ ${escapeHTML(w.id)}</td><td>${stateBadge(w.state)}</td><td>${escapeHTML(w.vm_id||'—')}</td><td>${w.next_step} / ${w.node.steps.length} steps</td></tr>`).join('')}`).join('')||'<tr><td colspan="4" class="empty-message">No workloads yet. Submit one to get started.</td></tr>';
  const sequence=data.events[0]?.seq??0;
  if(sequence!==eventSequence){
    const open=new Set(Array.from($('events').querySelectorAll('details[open]')).map(el=>el.dataset.seq));
    $('events').innerHTML=data.events.map(e=>`<details class="event" data-seq="${e.seq}" ${open.has(String(e.seq))?'open':''}><summary><time>${new Date(e.timestamp*1000).toLocaleTimeString()}</time>${escapeHTML(e.type.replaceAll('_',' '))}<p>${escapeHTML(e.task_id)}${e.worker_id?` / ${escapeHTML(e.worker_id)} · attempt ${e.attempt}`:''}</p></summary><pre>${escapeHTML(JSON.stringify(e.payload,null,2))}</pre></details>`).join('')||'<p class="empty-message">Activity will appear as workloads run.</p>';
    eventSequence=sequence;
  }
}
async function refresh(){
  try{render(await api('/dashboard'));$('error').hidden=true;$('connection').textContent=`● Connected · ${new Date().toLocaleTimeString()}`;}
  catch(error){$('connection').textContent='● Disconnected';$('error').textContent=`Unable to refresh: ${error.message}. Displayed data may be stale. Retrying automatically.`;$('error').hidden=false;}
}
$('submit-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('submit-button');button.disabled=true;$('submit-result').textContent='Submitting…';
  try{const spec=JSON.parse($('spec').value);const task_id=$('task-id').value.trim();if(!task_id)throw new Error('Enter a task ID.');const result=await api('/submit',{task_id,spec});$('submit-result').textContent=`Submitted ${result.task_id}. Use a new task ID to submit another workload.`;await refresh();}
  catch(error){$('submit-result').textContent=`Submission failed: ${error.message}`;}finally{button.disabled=false;}
});
async function poll(){await refresh();setTimeout(poll,1000);}poll();
