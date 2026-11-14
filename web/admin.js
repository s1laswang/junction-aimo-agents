var MOCK={"hotel-aurora":{customer:"hotel-aurora",agent_id:"buyer-aurora",per_order_eur:2500,per_week_eur:15000,exclude_allergens:[],allowed_categories:null,substitutions:"within_prefs",preferred_brands:["Valio"],approvers:["Laura Virtanen","Mikko Heikkinen"]},"hospital-tays":{customer:"hospital-tays",agent_id:"buyer-tays",per_order_eur:1500,per_week_eur:8000,exclude_allergens:["peanut","nuts","egg"],allowed_categories:null,substitutions:"ask",preferred_brands:["Valio"],approvers:["Helena Korhonen","Antti Lahtinen"]},"school-hervanta":{customer:"school-hervanta",agent_id:"buyer-hervanta",per_order_eur:600,per_week_eur:3000,exclude_allergens:["peanut","nuts"],allowed_categories:null,substitutions:"within_prefs",preferred_brands:["Valio"],approvers:["Sari Nieminen"]}};
var LIST=["exclude_allergens","allowed_categories","preferred_brands","approvers"];
var cur={},orig={};
var X='<svg class="i" viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M18 6L6 18M6 6l12 12"/></svg>';
function $(i){return document.getElementById(i)}
function render(){
$("who").textContent=cur.customer||aimo.customer();
$("agent").textContent=cur.agent_id||"agent";
$("per_order_eur").value=cur.per_order_eur;$("per_week_eur").value=cur.per_week_eur;$("substitutions").value=cur.substitutions;
LIST.forEach(function(k){
var box=document.querySelector('[data-k="'+k+'"]');
box.querySelectorAll(".chip").forEach(function(n){n.remove()});
var inp=box.querySelector("input");
(cur[k]||[]).forEach(function(v,i){
var c=document.createElement("span");c.className="chip";c.append(v);
var b=document.createElement("button");b.type="button";b.setAttribute("aria-label","Remove "+v);b.innerHTML=X;
b.onclick=function(){cur[k].splice(i,1);render()};c.append(b);box.insertBefore(c,inp)});
});
}
async function last(){
if(aimo.mockMode())return;
var a=await aimo.api("/v1/audit?customer="+encodeURIComponent(aimo.customer()),{admin:true}),e=(Array.isArray(a)?a:a&&a.events||[]).filter(function(x){return x.action==="mandate_updated"}).pop(),el=$("last");
el.textContent="";if(!e)return;
var k=document.createElement("a");k.href="/audit.html";k.className="mono";k.textContent="#"+e.seq;
el.append("Last change by "+e.actor+", "+new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Helsinki",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(e.ts)).replace(",","")+" Helsinki, audit ");el.append(k)}
async function load(){
var c=aimo.customer(),mk=aimo.mockMode(),m=mk?null:await aimo.api("/v1/mandate?customer="+encodeURIComponent(c),{admin:true});
aimo.mock(mk);
$("f").hidden=false;$("off").textContent="";
if(!mk&&!m){$("f").hidden=true;$("off").textContent=aimo.offline();$("last").textContent="";return}
cur=JSON.parse(JSON.stringify(m||MOCK[c]));
LIST.forEach(function(k){if(!cur[k])cur[k]=[]});
orig=JSON.parse(JSON.stringify(cur));
$("msg").textContent="";render();last();
}
document.querySelectorAll(".chips input").forEach(function(inp){
var k=inp.parentNode.dataset.k;
function add(){var v=inp.value.trim().replace(/,$/,"");if(v&&cur[k].indexOf(v)<0)cur[k].push(v);inp.value="";render();document.querySelector('[data-k="'+k+'"] input').focus()}
inp.addEventListener("keydown",function(e){if(e.key==="Enter"||e.key===","){e.preventDefault();add()}else if(e.key==="Backspace"&&!inp.value&&cur[k].length){cur[k].pop();render();document.querySelector('[data-k="'+k+'"] input').focus()}});
inp.addEventListener("blur",function(){if(inp.value.trim())add()});
});
$("reset").onclick=function(){cur=JSON.parse(JSON.stringify(orig));$("msg").textContent="";render()};
$("f").addEventListener("submit",async function(e){
e.preventDefault();
cur.per_order_eur=Number($("per_order_eur").value);cur.per_week_eur=Number($("per_week_eur").value);cur.substitutions=$("substitutions").value;
if(!(cur.per_order_eur>0)||!(cur.per_week_eur>0)){$("msg").textContent="Limits must be greater than zero.";return}
if(cur.per_order_eur>cur.per_week_eur){$("msg").textContent="Per order limit exceeds weekly limit.";return}
var body=Object.assign({},cur);if(!body.allowed_categories.length)body.allowed_categories=null;
var r=await aimo.api("/v1/mandate",{method:"PUT",body:body,admin:true});
var le=aimo.lastError;$("msg").textContent=r?"Saved.":(le&&le.status===404||le&&le.status===405)?"Not yet supported by gateway.":le&&le.status?"Blocked: "+(typeof le.detail==="string"?le.detail:"status "+le.status):"Gateway offline.";
if(r){orig=JSON.parse(JSON.stringify(cur));var a=await aimo.api("/v1/audit?customer="+encodeURIComponent(cur.customer),{admin:true}),l=(Array.isArray(a)?a:a&&a.events||[]).filter(function(e){return e.action==="mandate_updated"}).pop();last();if(l){$("msg").textContent="Saved, recorded in ";var k=document.createElement("a");k.href="/audit.html";k.textContent="audit #"+l.seq;$("msg").append(k)}}
});
window.addEventListener("customer",load);
load();
