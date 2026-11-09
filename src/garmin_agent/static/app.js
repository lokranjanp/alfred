'use strict';
const $ = s => document.querySelector(s);
const csrf = $('meta[name="csrf-token"]').content;
let prefs, foods = [], scheduleEnabled = false;
const tags = value => value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
function notice(text, error=false) { const n=$('#notice'); n.textContent=text; n.hidden=false; n.classList.toggle('error',error); }
async function api(path, options={}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  const data = await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : 'Check the form values and try again.');
  return data;
}
function setFields(form, values) { for(const [key,value] of Object.entries(values)){const field=form.elements.namedItem(key); if(field && 'value' in field) field.value=Array.isArray(value)?value.join(', '):(value??'');} }
async function loadPrefs() {
  prefs=await api('/api/preferences'); setFields($('#preferences-form'),prefs);
  setFields($('#connections-form'),{mcp_command:prefs.mcp.command,mcp_args:JSON.stringify(prefs.mcp.args),session_path:prefs.mcp.session_path,gemini_model:prefs.gemini_model,smtp_host:prefs.smtp.host,smtp_port:prefs.smtp.port,smtp_username:prefs.smtp.username,smtp_sender:prefs.smtp.sender,smtp_security:prefs.smtp.security});
}
async function refreshStatus() {
  const state=await api('/api/status'); scheduleEnabled=!!state.schedule.enabled;
  $('#timezone-label').textContent=state.timezone;
  $('#schedule-label').textContent=scheduleEnabled?'Daily, at 10 AM':'Not enabled';
  $('#schedule-toggle').textContent=scheduleEnabled?'Pause daily schedule':'Enable daily schedule';
  $('#next-run').textContent=scheduleEnabled?'Next: '+new Date(state.next_run).toLocaleString('en-GB',{timeZone:state.timezone,dateStyle:'medium',timeStyle:'short'}):'Complete setup to get started';
  $('#connection-count').textContent=Object.values(state.integration_tests).filter(x=>x.ok).length+' / 3';
  const results=$('#test-results'); results.replaceChildren();
  for(const [name,result] of Object.entries(state.integration_tests)) { const p=document.createElement('p');p.textContent=(result.ok?'✓ ':'○ ')+name+': '+(result.ok?'Connected':result.message);results.append(p); }
  $('#date-label').textContent=new Intl.DateTimeFormat('en-GB',{timeZone:state.timezone,weekday:'short',day:'numeric',month:'long'}).format(new Date());
}
function element(tag,text,className) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
async function loadFoods() {
  foods=await api('/api/foods'); const list=$('#food-list'); list.replaceChildren();
  if(!foods.length){const box=element('div',undefined,'panel empty');box.append(element('h3','Start with something you love.'),element('p','Add a few familiar foods or meals, with calories per serving.'));list.append(box);}
  for(const food of foods){const card=element('article',undefined,'panel food-card');card.append(element('h3',food.name),element('p',food.serving+' · '+food.calories+' kcal'),element('p',food.ingredients));const labels=element('div');for(const tag of [...food.diet_tags,...food.allergens.map(a=>'Contains: '+a)]) labels.append(element('span',tag,'tag'));card.append(labels);const actions=element('div',undefined,'actions');const edit=element('button','Edit','button secondary');edit.type='button';edit.onclick=()=>{setFields($('#food-form'),food);for(const check of $('#food-form').querySelectorAll('[name=meal_types]'))check.checked=food.meal_types.includes(check.value);$('#food-form-title').textContent='Edit favorite';$('#food-form').scrollIntoView({behavior:'smooth'});};const remove=element('button','Remove','button secondary');remove.type='button';remove.onclick=()=>act(remove,async()=>{await api('/api/foods/'+food.id,{method:'DELETE'});await loadFoods();notice('Food removed.');});actions.append(edit,remove);card.append(actions);list.append(card);}
}
async function act(button, operation) {button.disabled=true;try{await operation();}catch(error){notice(error.message,true);}finally{button.disabled=false;}}
for(const nav of document.querySelectorAll('[data-tab]')) nav.onclick=()=>{for(const node of document.querySelectorAll('.tab,.nav'))node.classList.remove('active');$('#'+nav.dataset.tab).classList.add('active');nav.classList.add('active');};
$('#preferences-form').onsubmit=event=>{event.preventDefault();act(event.submitter,async()=>{const d=Object.fromEntries(new FormData(event.currentTarget));const updated={...prefs,...d,activities:tags(d.activities),diet_tags:tags(d.diet_tags),allergies:tags(d.allergies),exercise_minutes:Number(d.exercise_minutes),sleep_goal_hours:Number(d.sleep_goal_hours),hydration_ml:d.hydration_ml?Number(d.hydration_ml):null};const result=await api('/api/preferences',{method:'PUT',body:JSON.stringify(updated)});await loadPrefs();await refreshStatus();notice('Preferences saved.'+(result.schedule_disabled?' Connections changed; re-enable scheduling after testing.':''));});};
$('#connections-form').onsubmit=event=>{event.preventDefault();act(event.submitter,async()=>{const form=event.currentTarget,d=Object.fromEntries(new FormData(form));let args;try{args=JSON.parse(d.mcp_args);}catch{throw new Error('MCP arguments must be a JSON array, such as ["-m", "garmin_mcp"].');}const updated={...prefs,gemini_model:d.gemini_model,mcp:{command:d.mcp_command,args,session_path:d.session_path},smtp:{host:d.smtp_host,port:Number(d.smtp_port),username:d.smtp_username,sender:d.smtp_sender||null,security:d.smtp_security}};await api('/api/preferences',{method:'PUT',body:JSON.stringify(updated)});for(const [name,value] of [['gemini',d.gemini_key],['smtp',d.smtp_password]])if(value)await api('/api/secrets/'+name,{method:'PUT',body:JSON.stringify({value})});form.elements.gemini_key.value='';form.elements.smtp_password.value='';await loadPrefs();await refreshStatus();notice('Connections saved. Test each connection, then enable your daily schedule.');});};
$('#food-form').onsubmit=event=>{event.preventDefault();act(event.submitter,async()=>{const form=event.currentTarget,fd=new FormData(form),d=Object.fromEntries(fd);const food={id:d.id,name:d.name,serving:d.serving,calories:Number(d.calories),ingredients:d.ingredients,diet_tags:tags(d.diet_tags),allergens:tags(d.allergens),meal_types:fd.getAll('meal_types')};await api('/api/foods'+(food.id?'/'+food.id:''),{method:food.id?'PUT':'POST',body:JSON.stringify(food)});form.reset();form.elements.id.value='';$('#food-form-title').textContent='Add a favorite';await loadFoods();notice('Saved to your food library.');});};
$('#cancel-food').onclick=()=>{$('#food-form').reset();$('#food-form').elements.id.value='';$('#food-form-title').textContent='Add a favorite';};
for(const button of document.querySelectorAll('[data-test]'))button.onclick=()=>act(button,async()=>{notice('Testing '+button.dataset.test+'…');try{await api('/api/integrations/'+button.dataset.test+'/test',{method:'POST'});notice('Connection successful.');}finally{await refreshStatus();}});
for(const button of document.querySelectorAll('[data-action]'))button.onclick=()=>act(button,async()=>{notice('Preparing your report. Garmin collection and analysis may take a few minutes…');const result=await api('/api/reports/run',{method:'POST',body:JSON.stringify({preview:button.dataset.action==='preview'})});notice(result.status==='uncertain'?'Delivery result is uncertain. Check your inbox and run history before taking action.':result.status==='sent'?'Today’s report has been sent.':'Preview ready. Open it in Recent reports.');if(window.htmx)htmx.trigger('#history','load');await refreshStatus();});
$('#schedule-toggle').onclick=()=>act($('#schedule-toggle'),async()=>{notice(scheduleEnabled?'Pausing schedule…':'Checking all connections before enabling…');await api('/api/schedule',{method:'POST',body:JSON.stringify({enabled:!scheduleEnabled})});await refreshStatus();notice(scheduleEnabled?'Daily schedule enabled. Keep this service running, or install its macOS LaunchAgent.':'Daily schedule paused.');});
Promise.all([loadPrefs(),loadFoods(),refreshStatus()]).catch(error=>notice(error.message,true));
