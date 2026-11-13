(function(){
var CUSTOMERS=["hotel-aurora","hospital-tays","school-hervanta"];
var KEYS={"hotel-aurora":"demo-hotel-aurora","hospital-tays":"demo-hospital-tays","school-hervanta":"demo-school-hervanta"};
function get(){try{return localStorage.getItem("aimo.customer")}catch(e){return null}}
function customer(){var c=get();return CUSTOMERS.indexOf(c)<0?CUSTOMERS[0]:c}
function setCustomer(c){try{localStorage.setItem("aimo.customer",c)}catch(e){}window.dispatchEvent(new CustomEvent("customer",{detail:c}))}
function agentKey(){try{return localStorage.getItem("aimo.agentKey")||KEYS[customer()]||null}catch(e){return KEYS[customer()]||null}}
async function api(path,opts){
opts=opts||{};var h=Object.assign({},opts.headers||{});
if(opts.admin){var ak;try{ak=localStorage.getItem("aimo.adminKey")}catch(e){}h["X-Admin-Key"]=ak||"demo-admin";delete opts.admin}else{var k=agentKey();if(k&&!h["X-Agent-Key"])h["X-Agent-Key"]=k}
if(opts.body&&typeof opts.body!=="string"){opts.body=JSON.stringify(opts.body);h["Content-Type"]="application/json"}
opts.headers=h;
window.aimo.lastError=null;try{var r=await fetch(path,opts);if(!r.ok){var d=null;try{var j=await r.json();d=j.detail||j}catch(e){}window.aimo.lastError={status:r.status,detail:d};return null}return await r.json()}catch(e){window.aimo.lastError={status:0,detail:"gateway offline"};return null}
}
function mock(on){var t=document.querySelector(".mocktag");if(t)t.classList.toggle("on",!!on)}
function init(){
var sel=document.getElementById("customer");
if(sel){sel.innerHTML=CUSTOMERS.map(function(c){return '<option value="'+c+'">'+c+'</option>'}).join("");sel.value=customer();sel.addEventListener("change",function(){setCustomer(sel.value)})}
var mb=document.querySelector(".menu-btn");
if(mb)mb.addEventListener("click",function(){var t=document.querySelector(".top");var o=t.classList.toggle("open");mb.setAttribute("aria-expanded",o)});
}
function mockMode(){return new URLSearchParams(location.search).has("mock")}
function offline(el){var e=window.aimo.lastError,t=e&&e.status?"Unavailable ("+e.status+")":"Gateway offline";if(el)el.innerHTML='<p class="empty muted">'+t+"</p>";return t}
window.aimo={mockMode:mockMode,offline:offline,lastError:null,customer:customer,setCustomer:setCustomer,api:api,mock:mock,CUSTOMERS:CUSTOMERS};
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);else init();
})();
