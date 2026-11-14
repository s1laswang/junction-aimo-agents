var MOCK={"hotel-aurora":{customer:"hotel-aurora",agent_id:"buyer-aurora",per_order_eur:2500,per_week_eur:15000,exclude_allergens:[],allowed_categories:null,substitutions:"within_prefs",preferred_brands:["Valio"],approvers:["Laura Virtanen","Mikko Heikkinen"]},"hospital-tays":{customer:"hospital-tays",agent_id:"buyer-tays",per_order_eur:1500,per_week_eur:8000,exclude_allergens:["peanut","nuts","egg"],allowed_categories:null,substitutions:"ask",preferred_brands:["Valio"],approvers:["Helena Korhonen","Antti Lahtinen"]},"school-hervanta":{customer:"school-hervanta",agent_id:"buyer-hervanta",per_order_eur:600,per_week_eur:3000,exclude_allergens:["peanut","nuts"],allowed_categories:null,substitutions:"within_prefs",preferred_brands:["Valio"],approvers:["Sari Nieminen"]}};
var LIST=["exclude_allergens","allowed_categories","preferred_brands","approvers"];
var cur={},orig={},mockSim={};
var X='<svg class="i" viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M18 6L6 18M6 6l12 12"/></svg>';
function $(i){return document.getElementById(i)}
function snap(m){
return JSON.stringify({
per_order_eur:Number.isFinite(m.per_order_eur)?m.per_order_eur:null,
per_week_eur:Number.isFinite(m.per_week_eur)?m.per_week_eur:null,
substitutions:m.substitutions||"",
exclude_allergens:m.exclude_allergens||[],
allowed_categories:m.allowed_categories||[],
preferred_brands:m.preferred_brands||[],
approvers:m.approvers||[]
});
}
function hasPendingChip(){
var p=false;
document.querySelectorAll(".chips input").forEach(function(inp){if(inp.value.trim())p=true});
return p;
}
function isDirty(){return snap(cur)!==snap(orig)||hasPendingChip()}
function syncDirty(){var r=$("reset");if(r)r.disabled=!isDirty()}
function parseNum(id){var raw=String($(id).value==null?"":$(id).value).trim();return raw===""?NaN:Number(raw)}
function syncInputs(){cur.per_order_eur=parseNum("per_order_eur");cur.per_week_eur=parseNum("per_week_eur");cur.substitutions=$("substitutions").value}
function errText(le){
if(!le||!le.status)return "Gateway offline.";
if(le.status===404||le.status===405)return "Not yet supported by gateway.";
var d=le.detail;
var t=typeof d==="string"?d:d&&typeof d.text==="string"?d.text:Array.isArray(d)&&d[0]&&d[0].msg?d[0].msg:"status "+le.status;
return "Blocked: "+t;
}
function render(){
$("who").textContent=cur.customer||aimo.customer();
$("agent").textContent=cur.agent_id||"agent";
$("per_order_eur").value=Number.isFinite(cur.per_order_eur)?String(cur.per_order_eur):"";
$("per_week_eur").value=Number.isFinite(cur.per_week_eur)?String(cur.per_week_eur):"";
$("substitutions").value=cur.substitutions||"within_prefs";
LIST.forEach(function(k){
var box=document.querySelector('[data-k="'+k+'"]');
box.querySelectorAll(".chip").forEach(function(n){n.remove()});
var inp=box.querySelector("input");
(cur[k]||[]).forEach(function(v,i){
var c=document.createElement("span");c.className="chip";c.append(v);
var b=document.createElement("button");b.type="button";b.setAttribute("aria-label","Remove "+v);b.innerHTML=X;
b.onclick=function(){syncInputs();cur[k].splice(i,1);render()};c.append(b);box.insertBefore(c,inp)});
});
syncDirty();
}
function auditLink(seq){
var k=document.createElement("a");
k.href="/audit.html#e"+seq;
k.className="mono";
k.textContent="#"+seq;
return k;
}
async function last(){
var el=$("last");el.textContent="";
if(aimo.mockMode())return null;
var a=await aimo.api("/v1/audit?customer="+encodeURIComponent(aimo.customer()),{admin:true}),e=(Array.isArray(a)?a:a&&a.events||[]).filter(function(x){return x.action==="mandate_updated"}).pop();
if(!e)return null;
el.append("Last change by "+e.actor+", "+new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Helsinki",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(e.ts)).replace(",","")+" Helsinki, audit ");
el.append(auditLink(e.seq));
return e;
}
async function load(){
var c=aimo.customer(),mk=aimo.mockMode(),m=mk?null:await aimo.api("/v1/mandate?customer="+encodeURIComponent(c),{admin:true});
aimo.mock(mk);
$("f").hidden=false;if($("sim-sec"))$("sim-sec").hidden=false;$("off").textContent="";
if(!mk&&!m){$("f").hidden=true;if($("sim-sec"))$("sim-sec").hidden=true;$("off").textContent=aimo.offline();$("last").textContent="";return}
cur=JSON.parse(JSON.stringify(m||MOCK[c]||MOCK["hotel-aurora"]));
LIST.forEach(function(k){if(!cur[k])cur[k]=[]});
orig=JSON.parse(JSON.stringify(cur));
document.querySelectorAll(".chips input").forEach(function(inp){inp.value=""});
$("msg").textContent="";render();await last();
}
["per_order_eur","per_week_eur"].forEach(function(id){
["input","change"].forEach(function(ev){
$(id).addEventListener(ev,function(){cur[id]=parseNum(id);syncDirty()});
});
});
["input","change"].forEach(function(ev){
$("substitutions").addEventListener(ev,function(){cur.substitutions=$("substitutions").value;syncDirty()});
});
document.querySelectorAll(".chips input").forEach(function(inp){
var k=inp.parentNode.dataset.k;
function add(){syncInputs();var v=inp.value.trim().replace(/,$/,"");if(v&&cur[k].indexOf(v)<0)cur[k].push(v);inp.value="";render();document.querySelector('[data-k="'+k+'"] input').focus()}
inp.addEventListener("keydown",function(e){if(e.key==="Enter"||e.key===","){e.preventDefault();add()}else if(e.key==="Backspace"&&!inp.value&&cur[k].length){syncInputs();cur[k].pop();render();document.querySelector('[data-k="'+k+'"] input').focus()}});
inp.addEventListener("input",syncDirty);
inp.addEventListener("blur",function(){if(inp.value.trim())add()});
});
$("reset").onclick=function(){
cur=JSON.parse(JSON.stringify(orig));
document.querySelectorAll(".chips input").forEach(function(inp){inp.value=""});
render();
$("msg").textContent="Reset to saved mandate.";
};
$("f").addEventListener("submit",async function(e){
e.preventDefault();
var added=false;
document.querySelectorAll(".chips input").forEach(function(inp){
var v=inp.value.trim().replace(/,$/,""),k=inp.parentNode.dataset.k;
if(v&&cur[k].indexOf(v)<0){cur[k].push(v);added=true}
inp.value="";
});
syncInputs();
if(added)render();else syncDirty();
if(!(Number.isFinite(cur.per_order_eur)&&Number.isFinite(cur.per_week_eur))||!(cur.per_order_eur>0)||!(cur.per_week_eur>0)){$("msg").textContent="Limits must be greater than zero.";return}
if(cur.per_order_eur>cur.per_week_eur){$("msg").textContent="Per order limit exceeds weekly limit.";return}
if(!cur.approvers||!cur.approvers.length){$("msg").textContent="At least one approver is required.";return}
var body=JSON.parse(JSON.stringify(cur));
if(!body.allowed_categories.length)body.allowed_categories=null;
if(aimo.mockMode()){
MOCK[cur.customer]=JSON.parse(JSON.stringify(body));
orig=JSON.parse(JSON.stringify(cur));
syncDirty();
$("msg").textContent="Saved (mock data).";
return;
}
var r=await aimo.api("/v1/mandate",{method:"PUT",body:body,admin:true});
$("msg").textContent=r?"Saved.":errText(aimo.lastError);
if(r){
orig=JSON.parse(JSON.stringify(cur));
syncDirty();
var l=await last();
if(l){$("msg").textContent="Saved, recorded in audit ";$("msg").append(auditLink(l.seq),".")}
}
});
$("sim-form").addEventListener("submit",async function(e){
e.preventDefault();
var pid=$("sim-product").value.trim(),amt=parseNum("sim-amount"),dt=$("sim-date").value.trim();
if(!pid){$("sim-msg").textContent="Product ID is required.";return}
if(!Number.isFinite(amt)||amt<0){$("sim-msg").textContent="Available amount must be zero or greater.";return}
var body={product_id:pid,available_amount:amt};
if(dt)body.available_date=dt;
if(aimo.mockMode()){
mockSim[pid]=body;
$("sim-msg").textContent="Simulated "+pid+" at "+amt+(dt?" on "+dt:"")+" (mock data).";
return;
}
var r=await aimo.api("/v1/admin/simulate",{method:"POST",body:body,admin:true});
$("sim-msg").textContent=r?"Simulated "+pid+" at "+amt+(dt?" on "+dt:"")+".":errText(aimo.lastError);
});
$("sim-clear").addEventListener("click",async function(){
if(aimo.mockMode()){
mockSim={};
$("sim-product").value="";$("sim-amount").value="0";$("sim-date").value="";
$("sim-msg").textContent="Simulation cleared (mock data).";
return;
}
var r=await aimo.api("/v1/admin/simulate",{method:"DELETE",admin:true});
if(r){$("sim-product").value="";$("sim-amount").value="0";$("sim-date").value=""}
$("sim-msg").textContent=r?"Simulation cleared.":errText(aimo.lastError);
});
window.addEventListener("customer",load);
load();
