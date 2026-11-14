(function(){
var Q=new URLSearchParams(location.search);
var MODE=Q.get("mock");
var $=function(id){return document.getElementById(id)};
var state={agent:"",q2o:{},events:[],text:"",view:"ledger",bad:null,cp:null,cpv:null,live:false,open:{}};
var ICON={
shield:'<svg class="i" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/></svg>',
alert:'<svg class="i" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M12 8v5M12 16h.01"/></svg>',
link:'<svg class="i" viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
chev:'<svg class="i chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>'
};

function canon(v){
if(v===null||typeof v!=="object")return JSON.stringify(v).replace(/[\u007f-\uffff]/g,function(c){return "\\u"+("0000"+c.charCodeAt(0).toString(16)).slice(-4)});
if(Array.isArray(v))return "["+v.map(canon).join(",")+"]";
return "{"+Object.keys(v).sort().map(function(k){return canon(k)+":"+canon(v[k])}).join(",")+"}";
}
async function sha(s){
var b=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(s));
return Array.from(new Uint8Array(b)).map(function(x){return x.toString(16).padStart(2,"0")}).join("");
}
var ZERO="0".repeat(64);
async function hashOf(e){return sha(e.prev_hash+canon({action:e.action,actor:e.actor,customer:e.customer,detail:e.detail,seq:e.seq,subject:e.subject,ts:e.ts,why:e.why}))}

async function verify(evs){
var s=evs.slice().sort(function(a,b){return a.seq-b.seq});
for(var i=0;i<s.length;i++){
var e=s[i];
if(await hashOf(e)!==e.hash)return {at:e.seq,why:"hash mismatch"};
if(i===0&&e.seq===1&&e.prev_hash!==ZERO)return {at:e.seq,why:"genesis prev_hash"};
if(i>0&&s[i-1].seq===e.seq-1&&s[i-1].hash!==e.prev_hash)return {at:e.seq,why:"prev_hash does not match #"+s[i-1].seq};
}
return null;
}

function b64u(s){
if(!/^[A-Za-z0-9_-]+$/.test(s))throw new Error("b64u");
var b=s.replace(/-/g,"+").replace(/_/g,"/");
while(b.length%4)b+="=";
var bin=atob(b),out=new Uint8Array(bin.length);
for(var i=0;i<bin.length;i++)out[i]=bin.charCodeAt(i);
return out;
}

async function verifyCheckpoint(cp,jwks,evs){
var tok=typeof cp==="string"?cp:cp&&cp.jws;
if(!tok||typeof tok!=="string")return {ok:false,why:"missing checkpoint jws"};
var parts=tok.split(".");
if(parts.length!==3||!parts[0]||!parts[1]||!parts[2])return {ok:false,why:"malformed checkpoint jws"};
var hdr,pay,sig;
try{
hdr=JSON.parse(new TextDecoder().decode(b64u(parts[0])));
pay=JSON.parse(new TextDecoder().decode(b64u(parts[1])));
sig=b64u(parts[2]);
}catch(e){return {ok:false,why:"malformed checkpoint jws"}}
if(!hdr||hdr.alg!=="EdDSA"||!hdr.kid)return {ok:false,why:"invalid jws header"};
if(!pay||typeof pay.seq!=="number"||typeof pay.hash!=="string")return {ok:false,why:"invalid checkpoint payload"};
if(cp&&typeof cp==="object"){
if((cp.seq!==undefined&&cp.seq!==pay.seq)||(cp.hash!==undefined&&cp.hash!==pay.hash)||(cp.ts!==undefined&&cp.ts!==pay.ts))return {ok:false,why:"checkpoint mismatch"};
}
if(Array.isArray(evs)){
if(pay.seq===0&&pay.hash!==ZERO)return {ok:false,why:"genesis checkpoint hash mismatch"};
var hit=evs.filter(function(e){return e.seq===pay.seq})[0];
if(hit&&hit.hash!==pay.hash)return {ok:false,why:"checkpoint hash does not match event #"+pay.seq};
}
if(!jwks||!Array.isArray(jwks.keys))return {ok:null,why:"missing jwks",head:pay};
var jwk=jwks.keys.filter(function(k){return k&&k.kid===hdr.kid&&k.kty==="OKP"&&k.crv==="Ed25519"&&typeof k.x==="string"})[0];
if(!jwk)return {ok:false,why:"unknown kid"};
if(sig.length!==64)return {ok:false,why:"bad signature"};
if(typeof crypto==="undefined"||!crypto||!crypto.subtle||typeof crypto.subtle.importKey!=="function")return {ok:null,unsupported:true,why:"WebCrypto unavailable",head:pay};
var key;
try{
key=await crypto.subtle.importKey("jwk",{kty:"OKP",crv:"Ed25519",x:jwk.x},{name:"Ed25519"},false,["verify"]);
}catch(e){
if(e&&(e.name==="DataError"||e.name==="SyntaxError"))return {ok:false,why:"invalid public key"};
return {ok:null,unsupported:true,why:"Ed25519 unsupported",head:pay};
}
try{
var ok=await crypto.subtle.verify({name:"Ed25519"},key,sig,new TextEncoder().encode(parts[0]+"."+parts[1]));
return ok?{ok:true,head:pay,kid:hdr.kid}:{ok:false,why:"bad signature"};
}catch(e){
return {ok:false,why:"bad signature"};
}
}

function directOrder(e){
var d=e.detail||{};if(d.order_id)return d.order_id;
var s=e.subject||"";
if(/^ord_/.test(s))return s;
var m=/^order:(.+)$/.exec(s);return m?m[1]:null;
}
function quoteOf(e){
var d=e.detail||{};if(d.quote_id)return String(d.quote_id).replace(/^quote:/,"");
var s=e.subject||"";
if(/^q_/.test(s))return s;
var m=/^quote:(.+)$/.exec(s);return m?m[1]:null;
}
function indexQuotes(evs){
state.q2o={};
evs.forEach(function(e){var o=directOrder(e),q=quoteOf(e);if(o&&q)state.q2o[q]=o});
}
function orderOf(e){
var o=directOrder(e);if(o)return o;
var q=quoteOf(e);return (q&&state.q2o[q])||null;
}

window.aimoAudit={verify:verify,hashOf:hashOf,verifyCheckpoint:verifyCheckpoint,orderOf:orderOf,indexQuotes:indexQuotes};

var MOCK_RAW=[
["hotel-aurora","agent:buyer-aurora","parse.request","hotel-aurora","2,500 l lactose-free milk for delivery Mon 16 Nov.",{text:"2500 litres lactose-free milk by Monday"}],
["hotel-aurora","agent:buyer-aurora","offers.compared","hotel-aurora","Compared 4 offers for 2,500 l lactose-free milk, best: Valio Eila lactose-free milk 1 l 2,800.00 EUR net, shortfall 0.",{offers:4,shortfall:0}],
["hotel-aurora","agent:buyer-aurora","quote.issued","q_91c2","Quote q_91c2 for 2,800.00 EUR net (3,192.00 EUR with VAT), valid until 14.11. 14:00 Helsinki time.",{quote_id:"q_91c2",total_net:2800,total_gross:3192,currency:"EUR"}],
["hotel-aurora","agent:buyer-aurora","quote.redeemed","q_91c2","Quote q_91c2 redeemed for 2,800.00 EUR net (3,192.00 EUR with VAT).",{quote_id:"q_91c2",total_net:2800,total_gross:3192,currency:"EUR"}],
["hotel-aurora","agent:buyer-aurora","order.placed","ord_7f3a","Agent submitted an order against a signed quote.",{order_id:"ord_7f3a",quote_id:"q_91c2",total_net:2800,lines:[{product_id:"410112",name:"Valio Eila lactose-free milk 1 l",quantity:2500}]}],
["hotel-aurora","system","order.pending_approval","ord_7f3a","Order total 2,800.00 EUR exceeds the 2,500.00 EUR per-order limit.",{order_id:"ord_7f3a",quote_id:"q_91c2",total_net:2800,reasons:[{code:"ORDER_OVER_LIMIT",text:"Order total 2,800.00 EUR exceeds the 2,500.00 EUR per-order limit."}]}],
["hotel-aurora","human:Laura Virtanen","approval.approved","ord_7f3a","Laura Virtanen approved the order of 2,800.00 EUR. Note: ok for Monday breakfast service. Held for approval because: Order total 2,800.00 EUR exceeds the 2,500.00 EUR per-order limit.",{order_id:"ord_7f3a",note:"ok for Monday breakfast service"}],
["hotel-aurora","system","order.confirmed","ord_7f3a","Confirmed for delivery Mon 16 Nov (simulated fulfilment, Valio has no order API).",{order_id:"ord_7f3a",quote_id:"q_91c2",confirmation_id:"MOCK-7F3A91C2",delivery_date:"2026-11-16",mock:true}],
["hotel-aurora","human:Laura Virtanen","mandate_updated","hotel-aurora","Mandate for hotel-aurora/buyer-aurora updated: per_order_eur 2000.0 -> 2500.0",{agent_id:"buyer-aurora",per_order_eur:2500,per_week_eur:15000}],
["hospital-tays","agent:buyer-tays","quote.issued","q_8b20","Quote q_8b20 for 296.00 EUR net (337.44 EUR with VAT), valid until 14.11. 14:00 Helsinki time.",{quote_id:"q_8b20",total_net:296,total_gross:337.44,currency:"EUR"}],
["hospital-tays","agent:buyer-tays","quote.redeemed","q_8b20","Quote q_8b20 redeemed for 296.00 EUR net (337.44 EUR with VAT).",{quote_id:"q_8b20",total_net:296,total_gross:337.44,currency:"EUR"}],
["hospital-tays","agent:buyer-tays","order.placed","ord_8b21","Agent submitted an order against a signed quote.",{order_id:"ord_8b21",quote_id:"q_8b20",total_net:296,lines:[{product_id:"400210",name:"Valio unsalted butter 500 g",quantity:80}]}],
["hospital-tays","system","order.confirmed","ord_8b21","Confirmed for delivery Mon 16 Nov (simulated fulfilment, Valio has no order API).",{order_id:"ord_8b21",quote_id:"q_8b20",confirmation_id:"MOCK-8B210044",delivery_date:"2026-11-16",mock:true}]
];
async function buildMock(broken){
var out=[],prev=ZERO,t=Date.parse("2026-11-13T09:00:00Z");
for(var i=0;i<MOCK_RAW.length;i++){
var r=MOCK_RAW[i],d=JSON.parse(JSON.stringify(r[5]));
var e={seq:i+1,ts:new Date(t+i*61000).toISOString().replace(/\.\d+Z$/,"Z"),customer:r[0],actor:r[1],action:r[2],subject:r[3],detail:d,why:r[4],prev_hash:prev};
e.hash=await hashOf(e);prev=e.hash;out.push(e);
}
if(broken)out[4].detail.total_net=1900;
return out;
}
function plainText(evs){
return evs.map(function(e){return "#"+e.seq+"  "+e.ts+"  "+e.actor+"  "+e.action+"\n    "+e.why+"\n    ref "+e.subject}).join("\n\n");
}

function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function short(h){return h?h.slice(0,8)+"\u2026"+h.slice(-4):"none"}
var HF=new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Helsinki",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
function tm(ts){var d=new Date(ts);return isNaN(d)?ts:HF.format(d)}
function dt(ts){var d=new Date(ts);return isNaN(d)?"":d.toISOString().slice(0,10)}

function filtered(){
var a=$("f-actor").value,x=$("f-action").value,c=$("f-customer").value,o=$("f-order").value.trim();
return state.events.filter(function(e){return (!c||e.customer===c)&&(!o||orderOf(e)===o)&&(!a||e.actor.split(":")[0]===a)&&(!state.agent||e.actor==="agent:"+state.agent)&&(!x||e.action===x)});
}
function updateCp(){
var box=$("cp-box");if(!box)return;
var cp=state.cp;
if(!cp||!cp.jws){box.hidden=true;return}
box.hidden=false;
if($("cp-seq"))$("cp-seq").textContent="#"+cp.seq;
if($("cp-jws")){$("cp-jws").textContent=cp.jws.length>40?cp.jws.slice(0,28)+"..."+cp.jws.slice(-8):cp.jws;$("cp-jws").title=cp.jws}
}
function badge(){
$("how-n").textContent=state.events.length;
var b=$("badge"),n=state.events.length;
if(state.off){b.className="badge idle";b.innerHTML=ICON.alert+"Not checked, "+aimo.offline().toLowerCase();return}
if(state.bad===undefined||state.bad==="pending"){b.className="badge idle";b.textContent="Verifying chain";return}
if(state.bad){b.className="badge stop";b.innerHTML=ICON.alert+'Chain broken at #'+state.bad.at+' <a href="#e'+state.bad.at+'" id="jump">Jump to #'+state.bad.at+'</a>';
$("jump").addEventListener("click",function(){state.open[state.bad.at]=true;render();var el=$("e"+state.bad.at);if(el)el.scrollIntoView({block:"center"})});return}
if(state.cpv&&state.cpv.ok===false){b.className="badge stop";b.innerHTML=ICON.alert+"Checkpoint signature invalid ("+esc(state.cpv.why||"bad signature")+")";return}
var cp=state.cp&&state.cp.jws?", checkpoint #"+state.cp.seq+(state.cpv&&state.cpv.ok===true?" Ed25519 verified":" signed"):"";
if(state.mode==="unknown"){b.className="badge idle";b.innerHTML=ICON.alert+"Chain check inconclusive in browser, "+n+" events";return}
b.className="badge ok";b.innerHTML=ICON.shield+(state.mode==="gateway"?"Chain verified by gateway, ":state.srv?"Chain verified in your browser and by gateway, ":"Chain verified in your browser, ")+n+" event"+(n===1?"":"s")+cp;if(state.cp&&state.cp.hash)b.title="checkpoint "+state.cp.hash+" at "+(state.cp.ts||"genesis");
}
var tseq=0;
async function scopedText(evs){
var my=++tseq,pl=$("plain"),c=$("f-customer").value,o=$("f-order").value.trim(),sub=$("f-actor").value||$("f-action").value||state.agent;
if(!state.live||sub){pl.textContent=evs.length?plainText(evs):"No audit events match these filters.";return}
pl.textContent="Loading";
var qs=[];if(c)qs.push("customer="+encodeURIComponent(c));if(o)qs.push("order_id="+encodeURIComponent(o));
var d=await aimo.api("/v1/audit"+(qs.length?"?"+qs.join("&"):""),{headers:{"X-Admin-Key":"demo-admin"}});
if(my!==tseq)return;
var t=d&&d.text;
pl.textContent=t&&(evs.length||!t)?t:(evs.length?plainText(evs):"No audit events match these filters.");
}
function legend(){
var c=$("f-customer").value,o=$("f-order").value.trim(),n={};
state.events.forEach(function(e){if(e.actor.indexOf("agent:")!==0||(c&&e.customer!==c)||(o&&orderOf(e)!==o))return;n[e.actor.slice(6)]=(n[e.actor.slice(6)]||0)+1});
var ids=Object.keys(n).sort(),on=$("f-actor").value==="agent";
$("legend").innerHTML='<button type="button" class="chip" data-id="" aria-pressed="'+(on&&!state.agent)+'">All agents</button>'+(ids.length?ids.map(function(i){return '<button type="button" class="chip" data-id="'+esc(i)+'" aria-pressed="'+(state.agent===i)+'"><span class="mono">'+esc(i)+'</span><span class="n">'+n[i]+'</span></button>'}).join(""):'<span class="muted small">No agent calls in this view.</span>');
}
function render(){
legend();
var evs=filtered(),L=$("ledger");
if(state.off){aimo.offline(L);$("plain").hidden=true;L.hidden=false;legend();return}
$("plain").hidden=state.view!=="text";L.hidden=state.view==="text";
$("v-ledger").setAttribute("aria-pressed",state.view==="ledger");$("v-text").setAttribute("aria-pressed",state.view==="text");
if(state.view==="text")scopedText(evs);
if(!evs.length){L.innerHTML='<div class="empty">No audit events match these filters.</div>';return}
var groups=[],idx={};
evs.forEach(function(e){var k=orderOf(e)||"";if(!(k in idx)){idx[k]=groups.length;groups.push({k:k,items:[]})}groups[idx[k]].items.push(e)});
var byHash={};state.events.forEach(function(e){byHash[e.seq]=e});
L.innerHTML=groups.map(function(g){
var first=g.items[0];
return '<div class="grp"><div class="grp-h"><h3>'+(g.k?"Order":"No order")+'</h3>'+(g.k?'<span class="mono">'+esc(g.k)+'</span>':'')+'<span class="small muted">'+dt(first.ts)+" &middot; "+g.items.length+' event'+(g.items.length===1?"":"s")+'</span></div>'+
g.items.map(function(e){
var bad=state.bad&&state.bad!=="pending"&&state.bad.at===e.seq,open=!!state.open[e.seq],kind=e.actor.split(":")[0];
var pe=byHash[e.seq-1];
return '<div class="row'+(bad?" bad":"")+'" id="e'+e.seq+'" data-open="'+open+'">'+
'<button type="button" class="line" aria-expanded="'+open+'" data-seq="'+e.seq+'"><span class="seq"><b>#'+e.seq+'</b></span><span class="ts">'+tm(e.ts)+'</span><span class="actor '+kind+'" title="'+esc(e.actor)+'">'+esc(e.actor)+'</span><span class="what"><span class="act">'+esc(e.action)+'</span><span class="why">'+esc(e.why)+'</span></span><span class="subj" title="'+esc(e.subject)+'">'+esc(e.subject)+'</span>'+ICON.chev+'</button>'+
'<div class="detail"><pre>'+esc(JSON.stringify(e.detail,null,2))+'</pre><div class="hashes"><span class="h'+(bad?" bad":"")+'" title="'+esc(e.prev_hash)+'">'+short(e.prev_hash)+'</span>'+ICON.link+'<span class="h'+(bad?" bad":"")+'" title="'+esc(e.hash)+'">'+short(e.hash)+'</span>'+(pe?'<span class="muted">follows #'+pe.seq+'</span>':'')+'</div></div></div>';
}).join("")+'</div>';
}).join("");
}
function fillActions(){
var sel=$("f-action"),cur=sel.value,acts=Array.from(new Set(state.events.map(function(e){return e.action}))).sort();
sel.innerHTML='<option value="">all</option>'+acts.map(function(a){return '<option value="'+esc(a)+'">'+esc(a)+'</option>'}).join("");
sel.value=acts.indexOf(cur)<0?"":cur;
}
async function load(){
state.off=false;state.bad="pending";state.cp=null;state.cpv=null;state.live=false;badge();updateCp();
var data=null;
if(!MODE)data=await aimo.api("/v1/audit",{headers:{"X-Admin-Key":"demo-admin"}});
var evs,text="";
if(data){evs=Array.isArray(data)?data:(data.events||data.trail||[]);text=Array.isArray(data)?"":(data.text||"");aimo.mock(false)}
else if(MODE){evs=await buildMock(MODE==="broken");aimo.mock(true)}
else{state.off=true;state.events=[];state.bad=null;badge();scope();render();$("receipt").hidden=true;return}
indexQuotes(evs);state.events=evs;state.text=text;state.cp=data&&!Array.isArray(data)?data.checkpoint:null;var sv=data&&!Array.isArray(data)?data.verified:null;
fillActions();
var br=await verify(evs);
if(state.cp&&state.cp.jws&&!MODE){
var jwks=await aimo.api("/.well-known/jwks.json");
state.cpv=await verifyCheckpoint(state.cp,jwks,evs);
}
state.bad=null;state.mode="browser";state.srv=!!(sv&&sv.ok);
if(sv&&sv.ok===false)state.bad={at:sv.bad_seq};
else if(br){if(sv&&sv.ok)state.mode="gateway";else if(data)state.mode="unknown";else state.bad=br}
state.live=!!data;
updateCp();badge();scope();render();receipt();
}
var MOCK_ORDERS={
ord_7f3a:{order_id:"ord_7f3a",quote_id:"q_91c2",customer:"hotel-aurora",agent_id:"buyer-aurora",total_net:2800,status:"confirmed",delivery_date:"2026-11-16",decision:{outcome:"needs_approval",reasons:[{code:"ORDER_OVER_LIMIT",text:"Order total 2,800.00 EUR exceeds the 2,500.00 EUR per-order limit."}]},substitutions:[{original_id:"410008",substitute_id:"410112",reason:"Substitute for Valio lactose-free milk 1 l: Valio Eila lactose-free milk 1 l."}],lines:[]},
ord_8b21:{order_id:"ord_8b21",quote_id:"q_8b20",customer:"hospital-tays",agent_id:"buyer-tays",total_net:296,status:"confirmed",delivery_date:"2026-11-16",decision:{outcome:"auto",reasons:[{code:"WITHIN_MANDATE",text:"Order total 296.00 EUR is within the 1,500.00 EUR per-order limit."}]},substitutions:[],lines:[]}
};
var TAG={confirmed:"ok",placed:"ok",approved:"ok",pending_approval:"warn",draft:"neutral",rejected:"stop",blocked:"stop",expired:"stop"};
function eur(v){return Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})+" EUR"}
function row(k,v){return '<div class="rc-k">'+k+'</div><div class="rc-v">'+v+'</div>'}
var rseq=0,rtimer=null;
async function receipt(){
var el=$("receipt"),id=$("f-order").value.trim(),my=++rseq;
if(!id||state.off){el.hidden=true;return}
var o=null;
if(state.live&&!MODE)o=await aimo.api("/v1/orders/"+encodeURIComponent(id),{headers:{"X-Admin-Key":"demo-admin"}});
else if(MODE&&MOCK_ORDERS[id])o=MOCK_ORDERS[id];
if(my!==rseq)return;
if(!o){el.hidden=false;el.innerHTML='<div class="panel-head"><h2>Order receipt</h2></div><p class="muted">No order found for '+esc(id)+'.</p>';return}
if(o.quote_id&&o.order_id&&!state.q2o[o.quote_id]){state.q2o[o.quote_id]=o.order_id;refilter()}
var evs=state.events.filter(function(e){return orderOf(e)===id||e.subject===o.quote_id||(e.detail&&e.detail.quote_id===o.quote_id)});
var ap=evs.filter(function(e){return e.actor.indexOf("human:")===0&&/^approval\./.test(e.action)}).pop()||evs.filter(function(e){return e.actor.indexOf("human:")===0&&/approv|reject/.test(e.action)}).pop();
var red=evs.some(function(e){return /redeem/.test(e.action)||e.action==="place_order"||/^order\./.test(e.action)});
var dec=o.decision||{};
var out={auto:"Placed automatically within the mandate",needs_approval:"Needed human approval",blocked:"Blocked by policy"}[dec.outcome]||dec.outcome||"";
var h='<div class="panel-head"><h2>Order receipt <span class="mono muted">'+esc(o.order_id)+'</span></h2><span class="tag '+(TAG[o.status]||"neutral")+'">'+esc(String(o.status).replace(/_/g," "))+'</span></div><div class="rc">';
h+=row("Customer",esc(o.customer))+row("Buyer agent",'<span class="mono">'+esc(o.agent_id)+'</span>')+row("Total net",'<span class="mono">'+eur(o.total_net)+'</span>')+row("Delivery",o.delivery_date?'<span class="mono">'+esc(String(o.delivery_date).slice(0,10))+'</span>':'<span class="muted">not set</span>');
h+=row("Decision",esc(out)+(dec.reasons&&dec.reasons.length?'<ul>'+dec.reasons.map(function(r){return '<li>'+esc(r.text||r.code)+'</li>'}).join("")+'</ul>':""));
var apNote=ap&&((ap.detail&&ap.detail.note)||((ap.why&&!/^(approved|rejected) by /i.test(ap.why))?ap.why:""));
h+=row("Approval",ap?esc(/reject/.test(ap.action)?"Rejected":"Approved")+" by <span class=\"mono\">"+esc(ap.actor.replace(/^human:/,""))+"</span> at <span class=\"mono\">"+esc(ap.ts.slice(0,10)+" "+tm(ap.ts)+" Helsinki")+"</span>"+(apNote?'<div class="muted small">'+esc(apNote)+'</div>':''):'<span class="muted">'+(dec.outcome==="needs_approval"?"Awaiting a human decision":"No human approval required")+'</span>');
var subs=o.substitutions||[];
h+=row("Substitutions",subs.length?'<ul>'+subs.map(function(x){return '<li><span class="mono">'+esc(x.original_id)+'</span> &rarr; <span class="mono">'+esc(x.substitute_id)+'</span>: '+esc(x.reason)+'</li>'}).join("")+'</ul>':'<span class="muted">none</span>');
h+=row("Quote",'<span class="mono">'+esc(o.quote_id)+'</span> '+(red?'<span class="tag ok">signature verified at placement</span>':'<span class="tag neutral">not yet redeemed</span>'));
el.innerHTML=h+'</div>';el.hidden=false;
}
function scope(){var c=$("f-customer").value,o=$("f-order").value.trim(),v=filtered().length,n=state.events.length;$("scope").textContent=(c||"all customers")+(o?" / "+o:"")+" \u00b7 showing "+v+" of "+n+" events"}
function refilter(){scope();render()}
function exportJson(){
var b=new Blob([JSON.stringify(state.events,null,2)],{type:"application/json"}),a=document.createElement("a");
a.href=URL.createObjectURL(b);a.download="audit-"+($("f-customer").value||"all")+".json";document.body.appendChild(a);a.click();a.remove();
}
$("f-order").value=Q.get("order_id")||"";
$("filters").addEventListener("submit",function(e){e.preventDefault()});
$("f-order").addEventListener("input",function(){
var v=this.value.trim(),u=new URL(location.href||String(location));
if(v)u.searchParams.set("order_id",v);else u.searchParams.delete("order_id");
history.replaceState(null,"",u);
refilter();
clearTimeout(rtimer);
if(!v||MODE)receipt();
else rtimer=setTimeout(receipt,180);
});
$("f-order").addEventListener("change",function(){clearTimeout(rtimer);receipt()});
["f-actor","f-action","f-customer"].forEach(function(i){$(i).addEventListener("change",function(){if(i==="f-actor")state.agent="";refilter()})});
$("legend").addEventListener("click",function(e){var b=e.target.closest(".chip");if(!b)return;var id=b.dataset.id;if(b.getAttribute("aria-pressed")==="true"){state.agent="";$("f-actor").value=""}else{state.agent=id;$("f-actor").value="agent"}refilter()});
$("v-ledger").addEventListener("click",function(){state.view="ledger";render()});
$("v-text").addEventListener("click",function(){state.view="text";render()});
$("export").addEventListener("click",exportJson);
$("print").addEventListener("click",function(){window.print()});
if($("cp-copy"))$("cp-copy").addEventListener("click",function(){
if(!state.cp||!state.cp.jws)return;
var btn=this,done=function(){btn.textContent="Copied";setTimeout(function(){btn.textContent="Copy JWS"},1200)};
if(typeof navigator!=="undefined"&&navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(state.cp.jws).then(done,done);else done();
});
$("ledger").addEventListener("click",function(e){var b=e.target.closest(".line");if(!b)return;var s=b.dataset.seq;state.open[s]=!state.open[s];var r=b.parentNode;r.dataset.open=!!state.open[s];b.setAttribute("aria-expanded",!!state.open[s])});
$("f-customer").innerHTML='<option value="">all customers</option>'+aimo.CUSTOMERS.map(function(c){return '<option value="'+c+'">'+c+"</option>"}).join("");
var qc=Q.get("customer");
$("f-customer").value=Q.has("customer")?(aimo.CUSTOMERS.indexOf(qc)>=0?qc:""):(Q.get("order_id")?"":aimo.customer());
window.addEventListener("customer",function(e){$("f-customer").value=e.detail;refilter()});
load();
})();
