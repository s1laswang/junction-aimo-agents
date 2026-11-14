(function(){
var CUSTOMERS=["hotel-aurora","hospital-tays","school-hervanta"];
var KEYS={"hotel-aurora":"demo-hotel-aurora","hospital-tays":"demo-hospital-tays","school-hervanta":"demo-school-hervanta"};
var keyBound=false;
function get(){
  try{return localStorage.getItem("aimo.customer")}catch(e){return null}
}
function syncUrlCustomer(){
  try{
    var q=new URLSearchParams(location.search).get("customer");
    if(q&&CUSTOMERS.indexOf(q)>=0){
      try{localStorage.setItem("aimo.customer",q)}catch(e){}
      return q;
    }
  }catch(e){}
  return null;
}
function customer(){
  var c=get();
  return CUSTOMERS.indexOf(c)<0?CUSTOMERS[0]:c;
}
function setCustomer(c){
  if(CUSTOMERS.indexOf(c)<0)return;
  try{localStorage.setItem("aimo.customer",c)}catch(e){}
  var sel=document.getElementById("customer");
  if(sel&&sel.value!==c)sel.value=c;
  var ev=typeof CustomEvent==="function"?new CustomEvent("customer",{detail:c}):{type:"customer",detail:c};
  window.dispatchEvent(ev);
}
function agentKey(){
  var k=null;
  try{k=localStorage.getItem("aimo.agentKey")}catch(e){}
  return k||KEYS[customer()]||null;
}
function errInfo(j,raw){
  if(j!=null){
    if(typeof j==="string")return{code:null,detail:j.trim()||null};
    var src=j.detail!==undefined&&j.detail!==null?j.detail:j;
    if(typeof src==="string")return{code:typeof j.code==="string"?j.code:null,detail:src};
    if(Array.isArray(src)){
      var msgs=src.map(function(x){
        if(typeof x==="string")return x;
        if(x&&typeof x==="object"){
          var loc=Array.isArray(x.loc)?x.loc.filter(function(p){return p!=="body"}).join("."):"";
          var m=x.text||x.msg||x.message||"";
          return loc&&m?loc+": "+m:m;
        }
        return"";
      }).filter(Boolean);
      return{code:null,detail:msgs.length?msgs.join("; "):null};
    }
    if(typeof src==="object"){
      var c=typeof src.code==="string"?src.code:(typeof j.code==="string"?j.code:null);
      var t=src.text||src.message||src.error||(typeof src.detail==="string"?src.detail:null)||c||null;
      return{code:c,detail:typeof t==="string"?t:null};
    }
  }
  if(typeof raw==="string"){
    var s=raw.trim();
    if(s&&s.charAt(0)!=="<")return{code:null,detail:s.slice(0,200)};
  }
  return{code:null,detail:null};
}
async function readPayload(r){
  if(r.status===204||r.status===205)return{empty:true,data:null,raw:""};
  if(typeof r.text==="function"){
    var txt=await r.text();
    if(!txt||!txt.trim())return{empty:true,data:null,raw:""};
    try{return{empty:false,data:JSON.parse(txt),raw:txt}}catch(e){return{empty:false,data:undefined,raw:txt}}
  }
  if(typeof r.json==="function"){
    try{var val=await r.json();return{empty:val===undefined,data:val,raw:""}}catch(e){return{empty:true,data:null,raw:""}}
  }
  return{empty:true,data:null,raw:""};
}
async function api(path,opts){
  var req=Object.assign({},opts||{});
  var h=Object.assign({},req.headers||{});
  if(req.admin){
    var ak=null;
    try{ak=localStorage.getItem("aimo.adminKey")}catch(e){}
    if(!h["X-Admin-Key"])h["X-Admin-Key"]=ak||"demo-admin";
    delete req.admin;
  }else{
    var k=agentKey();
    if(k&&!h["X-Agent-Key"])h["X-Agent-Key"]=k;
  }
  if(req.body!==undefined&&req.body!==null&&typeof req.body!=="string"){
    req.body=JSON.stringify(req.body);
    if(!h["Content-Type"])h["Content-Type"]="application/json";
  }
  req.headers=h;
  window.aimo.lastError=null;
  try{
    var r=await fetch(path,req);
    var p=await readPayload(r);
    if(!r.ok){
      var info=errInfo(p.data,p.raw);
      var det=info.detail;
      if(info.code&&det){
        det={code:info.code,text:det};
        Object.defineProperty(det,"toString",{value:function(){return info.detail}});
      }
      var le={status:r.status,detail:det};
      if(info.code)le.code=info.code;
      if(info.detail)le.text=info.detail;
      window.aimo.lastError=le;
      return null;
    }
    if(p.empty)return{};
    if(p.data===undefined){
      window.aimo.lastError={status:r.status||0,detail:"Invalid JSON response"};
      return null;
    }
    return p.data;
  }catch(e){
    window.aimo.lastError={status:0,detail:"gateway offline"};
    return null;
  }
}
function mock(on){
  var t=document.querySelector(".mocktag");
  if(t)t.classList.toggle("on",!!on);
}
function closeMenu(returnFocus){
  var t=document.querySelector(".top");
  if(!t||!t.classList.contains("open"))return false;
  t.classList.remove("open");
  var mb=document.querySelector(".menu-btn");
  if(mb){
    mb.setAttribute("aria-expanded","false");
    if(returnFocus&&typeof mb.focus==="function")mb.focus();
  }
  return true;
}
function onKeyDown(e){
  if(e&&(e.key==="Escape"||e.key==="Esc"))closeMenu(true);
}
function init(){
  syncUrlCustomer();
  var sel=document.getElementById("customer");
  if(sel){
    sel.innerHTML=CUSTOMERS.map(function(c){return'<option value="'+c+'">'+c+"</option>"}).join("");
    sel.value=customer();
    if(!sel._aimoBound){
      sel._aimoBound=true;
      sel.addEventListener("change",function(){setCustomer(sel.value)});
    }
  }
  var mb=document.querySelector(".menu-btn");
  if(mb&&!mb._aimoBound){
    mb._aimoBound=true;
    mb.addEventListener("click",function(){
      var t=document.querySelector(".top");
      if(!t)return;
      var o=t.classList.toggle("open");
      mb.setAttribute("aria-expanded",o?"true":"false");
    });
  }
  if(!keyBound){
    keyBound=true;
    if(document&&typeof document.addEventListener==="function")document.addEventListener("keydown",onKeyDown);
    if(window&&typeof window.addEventListener==="function")window.addEventListener("keydown",onKeyDown);
  }
}
function mockMode(){
  try{
    var p=new URLSearchParams(location.search);
    if(!p.has("mock"))return false;
    var v=String(p.get("mock")||"").trim().toLowerCase();
    return v!=="0"&&v!=="false";
  }catch(e){
    return false;
  }
}
function offline(el){
  var e=window.aimo.lastError;
  var st=e&&Number(e.status);
  var t=st?"Unavailable ("+st+")":"Gateway offline";
  if(el)el.innerHTML='<p class="empty muted">'+t+"</p>";
  return t;
}
syncUrlCustomer();
window.aimo={mockMode:mockMode,offline:offline,lastError:null,customer:customer,setCustomer:setCustomer,api:api,mock:mock,CUSTOMERS:CUSTOMERS};
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);else init();
})();
