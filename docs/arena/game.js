(function(){
const D=window.DATA;
const A={};D.attacks.attacks.forEach(a=>A[a.id]=a);
const ORDER=['price_change','allergen_substitute','impersonate_approver','prompt_injection_note','unknown_customer','replay_approval'];
const $=s=>document.querySelector(s);
const clean=s=>String(s).replace(/[\u2014\u2013]/g,'-');
const esc=s=>clean(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sh=h=>h.slice(0,12)+'...';
const short=t=>{t=clean(t).replace(/<[^>]+>/g,'');const w=t.split(/(?<=[.!?:])\s/)[0].split(' ');return w.length>8?w.slice(0,8).join(' ')+'...':w.join(' ')};
const who1=w=>String(w).split(/[,(]/)[0];
const W=320,H=192;
const cv=$('#cv'),g=cv.getContext('2d');
let K=2;
const IMG={};
let tokenN=0,skip=false,pending={},state='boot',now=0,shake=0,flash=null,banner='',lastT=0;
const score={tried:0,blocked:0,contained:0,miss:0,log:{}};

function loadImg(k,src){return new Promise((res,rej)=>{const i=new Image();i.onload=()=>{IMG[k]=i;res()};i.onerror=()=>rej(src);i.src=src})}

const bg=document.createElement('canvas');bg.width=W;bg.height=H;
function spr(ctx,k,idx,x,y,flip){
  const sx=(idx%12)*16,sy=Math.floor(idx/12)*16;
  if(flip){ctx.save();ctx.translate(x+16,y);ctx.scale(-1,1);ctx.drawImage(IMG[k],sx,sy,16,16,0,0,16,16);ctx.restore()}
  else ctx.drawImage(IMG[k],sx,sy,16,16,x,y,16,16);
}
function buildBg(){
  const b=bg.getContext('2d');b.imageSmoothingEnabled=false;
  for(let ty=0;ty<12;ty++)for(let tx=0;tx<20;tx++){
    if(tx>=16){b.fillStyle=(tx+ty)%2?'#6fbf5b':'#69b956';b.fillRect(tx*16,ty*16,16,16)}
    else if(ty<2)spr(b,'d',40,tx*16,ty*16);
    else spr(b,'d',[48,48,49,48,53,48,49][(tx*7+ty*13)%7],tx*16,ty*16);
  }
  b.fillStyle='#d9a066';b.fillRect(256,88,64,56);
  b.fillStyle='#c48b53';for(let i=0;i<14;i++)b.fillRect(260+(i*37)%56,92+(i*23)%48,2,2);
  for(let ty=0;ty<12;ty++){if(ty<6||ty>8)spr(b,'d',40,240,ty*16)}
  spr(b,'d',76,240,96);spr(b,'d',76,240,112);
  [[6,1],[7,1],[8,1],[11,1],[12,1]].forEach(p=>{spr(b,'d',63,p[0]*16,p[1]*16);spr(b,'d',75,p[0]*16,p[1]*16+16)});
  spr(b,'t',4,266,6);spr(b,'t',16,266,22);spr(b,'t',3,292,40);spr(b,'t',15,292,56);spr(b,'t',5,300,168);spr(b,'t',4,262,150);spr(b,'t',16,262,166);
  spr(b,'d',72,16,152);spr(b,'d',72,32,152);spr(b,'d',89,64,34);spr(b,'d',91,96,34);spr(b,'d',66,200,150);spr(b,'d',73,176,100);
  spr(b,'d',72,192,64);spr(b,'d',72,208,64);
  spr(b,'d',66,284,140);spr(b,'d',63,264,100);spr(b,'d',75,264,116);
  spr(b,'d',73,128,156);
}

const HOME={buyer:{x:44,y:150},agent:{x:132,y:122},approver:{x:208,y:76},supplier:{x:284,y:118},led:{x:152,y:44}};
const cast={
  buyer:{i:85,n:'BUYER',lc:'#4cc9f0'},
  agent:{i:84,n:'AI AGENT',lc:'#c9a0ff'},
  approver:{i:97,n:'ANNA',lc:'#5dd39e'},
  supplier:{i:86,n:'SUPPLIER',lc:'#ffd166'},
  atk:{i:110,n:'YOU',lc:'#ff5d73',vis:false}
};
const fxP=[],pops=[],items=[];
function resetCast(){
  for(const k of ['buyer','agent','approver','supplier']){const a=cast[k];a.x=HOME[k].x;a.y=HOME[k].y;a.tx=null;a.fl=k==='buyer'||k==='supplier'?false:false;a.em=null;a.kick=0}
  cast.atk.vis=false;cast.atk.x=-20;cast.atk.y=156;cast.atk.tx=null;cast.atk.em=null;cast.atk.i=110;cast.atk.n='YOU';
  fxP.length=0;pops.length=0;items.length=0;banner='';
}
function emote(k,t,ms){cast[k].em={t,until:now+(ms||1400)}}
function burst(x,y,col,n){for(let i=0;i<(n||14);i++){const a=Math.random()*6.28,s=20+Math.random()*50;fxP.push({x,y,vx:Math.cos(a)*s,vy:Math.sin(a)*s-20,life:.6+Math.random()*.4,col,s:1+Math.floor(Math.random()*2)})}}
function pop(t,x,y,col){pops.push({t,x,y,col,life:1.2})}
function move(k,x,y,sp){return new Promise(r=>{const a=cast[k];a.tx=x;a.ty=y;a.sp=sp||56;a.done=r})}
function fly(idx,img,from,to,ms,arc){
  return new Promise(r=>{const it={idx,img,x:from.x,y:from.y,t0:now,ms:(skip?60:ms)/1000,from,to,arc:arc||14,r};items.push(it)});
}
function pos(k){const a=cast[k]||HOME[k];return{x:a.x,y:a.y-10}}

function update(dt){
  for(const k in cast){
    const a=cast[k];
    if(a.tx!=null){
      const dx=a.tx-a.x,dy=a.ty-a.y,d=Math.hypot(dx,dy),st=a.sp*(skip?4:1)*dt;
      a.mv=true;
      if(d<=st){a.x=a.tx;a.y=a.ty;a.tx=null;a.mv=false;if(a.done){const f=a.done;a.done=null;f()}}
      else{a.x+=dx/d*st;a.y+=dy/d*st;if(Math.abs(dx)>1)a.fl=dx<0}
    }else a.mv=false;
    if(a.kick){a.x+=a.kick*dt*60;a.kick*=.86;if(Math.abs(a.kick)<.05)a.kick=0}
  }
  for(let i=fxP.length-1;i>=0;i--){const p=fxP[i];p.life-=dt;p.x+=p.vx*dt;p.y+=p.vy*dt;p.vy+=90*dt;if(p.life<=0)fxP.splice(i,1)}
  for(let i=pops.length-1;i>=0;i--){const p=pops[i];p.life-=dt;p.y-=14*dt;if(p.life<=0)pops.splice(i,1)}
  for(let i=items.length-1;i>=0;i--){const it=items[i],u=Math.min(1,(now-it.t0)/it.ms);
    it.x=it.from.x+(it.to.x-it.from.x)*u;it.y=it.from.y+(it.to.y-it.from.y)*u-Math.sin(u*Math.PI)*it.arc;
    if(u>=1){items.splice(i,1);it.r()}}
  if(shake>0)shake=Math.max(0,shake-dt*14);
}
function label(t,x,y,col){
  g.font='bold 6px "Courier New",monospace';g.textAlign='center';g.textBaseline='middle';
  const w=g.measureText(t).width+4;
  g.fillStyle='rgba(10,8,20,.82)';g.fillRect(Math.round(x-w/2),Math.round(y-4),Math.round(w),8);
  g.fillStyle=col;g.fillText(t,Math.round(x),Math.round(y));
}
function draw(){
  g.setTransform(K,0,0,K,0,0);g.imageSmoothingEnabled=false;
  g.clearRect(0,0,W,H);
  g.save();
  if(shake>0)g.translate((Math.random()-.5)*shake*2,(Math.random()-.5)*shake*2);
  g.drawImage(bg,0,0);
  const t=now;
  g.save();spr(g,'d',89,HOME.led.x-8,HOME.led.y-8+Math.sin(t*3)*0.5);g.restore();
  const LB=state==='scam'||state==='order';
  if(LB){label('AUDIT LEDGER',HOME.led.x,HOME.led.y-14,'#4cc9f0');label('DOCK',288,10,'#ffd166');label('HOTEL AURORA',60,112,'#a79fb8')}
  const list=Object.keys(cast).filter(k=>cast[k].vis!==false).sort((a,b)=>cast[a].y-cast[b].y);
  for(const k of list){
    const a=cast[k];
    g.fillStyle='rgba(0,0,0,.28)';g.fillRect(Math.round(a.x-6),Math.round(a.y-1),12,3);
    let bob=0;
    if(a.mv)bob=Math.sin(t*20)>0?-1:0;else bob=Math.sin(t*3+a.x)>0.6?-1:0;
    const fl=a.mv?(Math.sin(t*10)>0?!a.fl:a.fl):a.fl;
    spr(g,'d',a.i,Math.round(a.x-8),Math.round(a.y-16+bob),fl);
    if(LB)label(a.n,a.x,a.y-21+bob,a.lc);
    if(a.em&&a.em.until>t){
      const ex=a.x+14,ey=a.y-30+Math.round(Math.sin(t*8));
      g.fillStyle='#fff';g.fillRect(Math.round(ex-6),Math.round(ey-5),12,10);g.fillRect(Math.round(ex-1),Math.round(ey+5),2,2);
      g.fillStyle='#14121f';g.font='bold 8px "Courier New",monospace';g.textAlign='center';g.textBaseline='middle';g.fillText(a.em.t,Math.round(ex),Math.round(ey));
    }
  }
  for(const it of items){spr(g,it.img||'d',it.idx,Math.round(it.x-8),Math.round(it.y-8),false)}
  for(const p of fxP){g.fillStyle=p.col;g.fillRect(Math.round(p.x),Math.round(p.y),p.s,p.s)}
  for(const p of pops){
    g.globalAlpha=Math.min(1,p.life*2);g.font='bold 8px "Courier New",monospace';g.textAlign='center';g.textBaseline='middle';
    g.fillStyle='#000';g.fillText(p.t,Math.round(p.x)+1,Math.round(p.y)+1);g.fillStyle=p.col;g.fillText(p.t,Math.round(p.x),Math.round(p.y));g.globalAlpha=1}
  if(banner){
    g.fillStyle='rgba(10,8,20,.78)';g.fillRect(0,0,W,9);
    g.fillStyle='#f4efe6';g.font='bold 6px "Courier New",monospace';g.textAlign='left';g.textBaseline='middle';g.fillText(clean(banner),4,5);
  }
  g.restore();
  if(flash&&flash.until>now){g.globalAlpha=Math.min(.45,(flash.until-now)*1.5);g.fillStyle=flash.col;g.fillRect(0,0,W,H);g.globalAlpha=1}
}
function frame(ts){
  now=ts/1000;const dt=Math.min(.05,now-lastT||0.016);lastT=now;
  update(dt);draw();requestAnimationFrame(frame);
}
function fit(){
  const w=$('#stage').clientWidth,dpr=window.devicePixelRatio||1;
  K=Math.max(1,w*dpr/W);cv.width=Math.round(W*K);cv.height=Math.round(H*K);cv.style.width=(W*K/dpr)+'px';cv.style.height=(H*K/dpr)+'px';
}
window.addEventListener('resize',fit);

const wait=async ms=>{const t=tokenN;await new Promise(r=>setTimeout(r,skip?20:ms));if(t!==tokenN)throw 'abort'};
const waitAct=name=>new Promise(r=>{pending[name]=r});
function newRun(){tokenN++;skip=false;pending={};cap('')}
async function run(fn){const t=tokenN;try{await fn()}catch(e){if(e!=='abort')console.error(e)}}

function scoreHtml(){
  const done=ORDER.filter(id=>score.log[id]&&score.log[id].size).length;
  $('#score').innerHTML=`<span class="chip t">TRIED <i>${score.tried}</i></span><span class="chip b">BLOCKED <i>${score.blocked}</i></span><span class="chip c">CAUGHT <i>${score.contained}</i></span><span class="chip m">HOLES <i>${score.miss}</i></span><span class="chip">BAD ORDERS <i>0</i></span><button class="btn sm" data-act="menu">MENU</button><button class="btn sm yl" data-act="info" aria-label="Info" title="Info">i</button>`;
}

function portrait(el,k){
  const c=el.querySelector('canvas');if(!c)return;
  const x=c.getContext('2d');x.imageSmoothingEnabled=false;x.clearRect(0,0,16,16);
  const idx=typeof k==='number'?k:cast[k].i;
  x.drawImage(IMG.d,(idx%12)*16,Math.floor(idx/12)*16,16,16,0,0,16,16);
}
const sayHtml=(who,txt)=>`<div class="say"><canvas width="16" height="16"></canvas><div><div class="who">${esc(who)}</div><div class="tx">${short(txt)}</div><div class="inf">${txt}</div></div></div>`;
function cap(t){const c=$('#cap');c.textContent=t?clean(t):'';c.classList.toggle('on',!!t)}
function setSide(html){$('#side').innerHTML=html}
function ov(html){const o=$('#ov');o.innerHTML=html;o.classList.toggle('on',!!html)}
function sayPortrait(k){portrait($('#side'),k)}

document.addEventListener('click',e=>{
  const b=e.target.closest('[data-act]');if(!b)return;
  const a=b.dataset.act,v=b.dataset.v;
  if(a==='menu'){toMode();return}
  if(a==='info'){document.body.classList.toggle('info');return}
  if(a==='skip'){skip=true;return}
  if(pending[a]){const f=pending[a];delete pending[a];f(v);return}
  if(ACTS[a])ACTS[a](v);
});
const ACTS={};

function toTitle(){
  newRun();state='title';resetCast();cast.atk.vis=true;cast.atk.x=20;cast.atk.y=170;banner='';
  scoreHtml();
  ov(`<h1>SCAM <b>THE</b> AGENT</h1><div class="tag">AI buys milk. You scam it.</div><button class="btn go pulse big" data-act="scam">SCAM IT</button><button class="btn gr" data-act="order">WATCH FIRST</button>`);
  $('#foot').textContent='Sources: '+D.cast.source;
  setSide(`<div class="pn"><h2>How</h2><div class="list"><div><span>1 Pick</span></div><div><span>2 Watch</span></div><div><span>3 Learn</span></div></div><div class="dim sub inf">Goal: get a bad order placed. Every answer is a real recorded response from the gateway code and its tests.</div></div>`);
}
ACTS.mode=()=>toScam();
function toMode(){
  newRun();state='mode';resetCast();cast.atk.vis=true;cast.atk.x=30;cast.atk.y=172;banner='';
  scoreHtml();
  const cards=`<div class="modes"><button class="mode" data-act="order"><h3>ORDER</h3><p>Watch, 30 s</p></button><button class="mode scam" data-act="scam"><h3>SCAM</h3><p>Be the thief</p></button></div>`;
  const narrow=window.innerWidth<600;
  if(narrow){ov('');setSide(`<div class="pn"><h2>Choose</h2>${cards}</div><div class="pn"><div class="dim">Tip: ORDER first.</div></div>`)}
  else{ov(`<h1 style="font-size:clamp(20px,4vw,36px)">CHOOSE</h1>${cards}`);
  setSide(`<div class="pn"><div class="dim">Tip: ORDER first.</div></div>`)}
}

const ledgerRow=(e,cls)=>`<div class="ev ${cls||''}"><b>#${e.seq} ${esc(e.action)}</b><div class="inf"><span class="dim">actor ${esc(e.actor)}</span><br>${esc(e.summary)}<br><span class="hash">prev ${e.prev_hash.slice(0,10)} -> hash ${e.hash.slice(0,10)}</span></div></div>`;

async function orderMode(){
  newRun();state='order';resetCast();ov('');scoreHtml();
  const hp=D.happy_path,S={};hp.steps.forEach(s=>S[s.id]=s);
  banner=D.cast.customers[0].name+'  '+D.cast.customers[0].policy_version;
  let ledger=[],card='',dlg='',btn='',dk='buyer';
  const render=()=>{
    setSide(`<div class="pn">${dlg}<div class="row">${btn}</div></div>${card?`<div class="pn">${card}</div>`:''}<div class="pn"><h2>Ledger</h2><div class="led">${ledger.join('')||'<span class="dim">empty</span>'}</div><div class="src inf">docs/evidence/live-run/audit-events.json. ${esc(hp.audit_chain.hash_rule)}</div></div>`);
    sayPortrait(dk);
  };
  const say=(k,who,txt)=>{dk=k;dlg=sayHtml(who,txt);cap(who+': '+txt.replace(/&quot;/g,'"'));render()};
  await run(async()=>{
    say('buyer','Buyer','Ask for milk.');
    card=`<h2>Request</h2><div class="inf">${esc(hp.request_text)}</div>`;
    btn=`<button class="btn go pulse" data-act="send">SEND</button>`;render();
    await waitAct('send');btn='';
    emote('buyer','!');
    say('buyer','Buyer',esc(hp.request_text));
    ledger.push(`<div class="ev dm"><b>request.received</b><br>${esc(S.request.note)}</div>`);render();
    await fly(101,'d',pos('buyer'),pos('agent'),900);
    await wait(300);
    emote('agent','?',1600);
    say('agent','AI agent',esc(S.plan.text));
    card=`<h2>Plan</h2><div class="inf"><div class="list">${S.plan.evidence.map(x=>`<div><span>${esc(x)}</span></div>`).join('')}</div><div class="src">docs/evidence/live-run/run.json</div></div>`;render();
    await wait(2600);
    await fly(101,'d',pos('agent'),pos('supplier'),900);
    burst(cast.supplier.x,cast.supplier.y-12,'#ffd166',16);
    await fly(101,'d',pos('supplier'),pos('agent'),900);
    const q=hp.quote;
    emote('supplier','$');
    say('supplier','Supplier','Quote signed. Locked '+q.price_lock_minutes+' min.');
    card=`<h2>Quote</h2><div class="kv"><span>qty</span><span>${q.line.qty_sales_units} x EUR ${q.line.unit_price.toFixed(2)}</span><span>total</span><span class="ok">EUR ${q.total_net.toFixed(2)}</span></div><div class="inf"><div class="kv"><span>id</span><span>${esc(q.quote_id)}</span><span>product</span><span>${esc(q.line.product_name)}</span><span>signature</span><span>${esc(q.jws_alg)} (${esc(q.jws_typ)})</span><span>quote_hash</span><span class="hash">${q.quote_hash}</span></div><div class="src">docs/evidence/live-run/quote.json</div></div>`;
    const e1=hp.audit_events[0];ledger.push(ledgerRow(e1));render();
    burst(HOME.led.x,HOME.led.y,'#4cc9f0',12);pop('#1',HOME.led.x,HOME.led.y-18,'#4cc9f0');
    await wait(2200);
    const pd=hp.policy_decision;
    emote('supplier','#');
    say('supplier','Policy','Checking the rules.');
    card=`<h2>Rules</h2><div class="hits">${pd.hits.map((h,i)=>`<div class="hit ${pd.flagged_rules.includes(h.rule_id)?'fl':''}" style="animation-delay:${i*0.14}s"><i class="d"></i><span class="n">${esc(h.rule_id)}</span><span class="inf">${esc(h.message)}</span></div>`).join('')}</div><div class="warn" style="margin-top:6px">${esc(pd.outcome)}</div><div class="src inf">Flagged: ${esc(pd.flagged_rules.join(', '))}. docs/evidence/live-run/quote.json policy_decision</div>`;
    ledger.push(ledgerRow(hp.audit_events[1]));render();
    await wait(2400);
    flash={col:'#ffd166',until:now+.4};pop('NEEDS HUMAN',cast.agent.x,cast.agent.y-36,'#ffd166');
    ledger.push(ledgerRow(hp.audit_events[2]));
    emote('approver','!',2500);
    say('agent','Agent','Needs a human. Asking Anna.');
    btn=`<button class="btn go pulse" data-act="approve">APPROVE</button>`;
    card=`<h2>Waiting: ${esc(hp.approval.approved_by)}</h2><div class="inf"><div class="kv"><span>role</span><span>${esc(hp.approval.approver_role)}</span><span>approves hash</span><span class="hash">${sh(q.quote_hash)}</span></div></div>`;
    render();
    await waitAct('approve');btn='';
    await fly(101,'d',pos('agent'),pos('approver'),700);
    flash={col:'#5dd39e',until:now+.4};burst(cast.approver.x,cast.approver.y-12,'#5dd39e',18);pop('APPROVED',cast.approver.x,cast.approver.y-36,'#5dd39e');
    say('approver','Anna','Approved. Hash '+sh(q.quote_hash)+'. Note: "'+esc(hp.approval.note)+'"');
    ledger.push(ledgerRow(hp.audit_events[3]));ledger.push(ledgerRow(hp.audit_events[4]));render();
    await wait(2000);
    await fly(101,'d',pos('approver'),pos('supplier'),900);
    flash={col:'#5dd39e',until:now+.5};burst(cast.supplier.x,cast.supplier.y-12,'#ffd166',24);pop('PLACED AP',cast.supplier.x,cast.supplier.y-36,'#5dd39e');
    emote('supplier','AP',2000);emote('buyer','OK',2000);
    const o=hp.order;
    say('supplier','Supplier','Order '+esc(o.order_id)+' placed. Response '+esc(o.response_code)+' ('+esc(o.response_meaning[o.response_code])+').');
    ledger.push(ledgerRow(hp.audit_events[5]));
    card=`<h2>Order</h2><div class="kv"><span>id</span><span>${esc(o.order_id)}</span><span>status</span><span class="ok">${esc(o.status)}</span></div><div class="inf"><div class="kv"><span>chain</span><span>${esc(hp.audit_chain.append_only)}</span></div><div class="src">docs/evidence/live-run/order.json, ${esc(hp.audit_chain.source)}</div></div>`;
    btn=`<button class="btn no" data-act="scam">SCAM IT</button><button class="btn gr" data-act="order">REPLAY</button>`;
    render();
  });
}
ACTS.order=()=>run(orderMode);

const FILL=(t,m)=>t.replace(/\{(\w+)(![rs])?\}/g,(x,k)=>m[k]!==undefined?m[k]:x);
function res(head,lines,msg){return{head,lines:lines||[],msg}}

function buildMoves(id){
  const a=A[id];
  if(id==='price_change'){
    const q=D.attack_price_change_quote,st={};q.stages.forEach(s=>st[s.id]=s);
    const edit=`Changes unit_price ${q.original.unit_price.toFixed(2)} to ${q.tampered.unit_price.toFixed(2)}. Line total ${q.original.total_net} becomes ${q.tampered.total_net}.`;
    return[
      {id:'edit',title:'Edit the price, keep the signature',sub:'1.01 to 1.51 per litre',beats:[
        {who:'You, rogue supplier',say:edit,anim:'edit'},
        {who:'verify_quote_detailed',say:'Anyone can check the quote offline.',res:res('hash_matches: false',st.edit_only.reasons),anim:'block',at:'agent'}]},
      {id:'forge',title:'Edit the price and recompute the hash',sub:'smarter, forge the hash too',beats:[
        {who:'You, rogue supplier',say:edit+' Then recompute quote_hash: '+sh(q.tampered.recomputed_hash)+' (was '+sh(q.original.quote_hash)+').',anim:'edit'},
        {who:'verify_quote_detailed',say:'Hash matches now, but the signature does not.',res:res('hash_matches: true, signature_valid: false',st.forge_hash.reasons.concat([st.forge_hash.why])),anim:'block',at:'agent'},
        {who:'Gateway, POST /v1/orders',say:'You submit the forged hash anyway.',res:res(st.submit_forged_hash.http+' '+st.submit_forged_hash.body.error,[st.submit_forged_hash.body.message,'expected '+sh(st.submit_forged_hash.body.detail.expected),'order_created: '+st.submit_forged_hash.order_created]),anim:'block',at:'supplier'}]},
      {id:'body',title:'Send the real hash with a different body',sub:'hope the gateway reads your body',beats:[
        {who:'You, rogue supplier',say:'POST /v1/orders with the original hash but a body that says 1.51.',anim:'send',at:'supplier'},
        {who:'Gateway',say:'It never reads your body.',res:res('stored price wins',[st.submit_original_hash.result]),anim:'block',at:'supplier'}]}
    ].map(m=>Object.assign(m,{code:a.code,test:a.test,audit:null,auditNote:a.audit_note,verdict:'blocked'}));
  }
  if(id==='allergen_substitute'){
    const r=a.responses;
    return[
      {id:'milk',title:'Quote a milk drink as the substitute',sub:'school is milk-free and nut-free',beats:[
        {who:'You, sneaky supplier',say:'Swap the oat drink for 464306 and claim it is safe in the line flags.',anim:'send',at:'agent'},
        {who:'Policy engine, '+r[0].rule_id,say:'Allergen data comes from supplier structured data, never from what you claim.',res:res('order_status: '+r[0].order_status+', decision: '+r[0].decision_outcome,[r[0].message]),anim:'block',at:'supplier'}]},
      {id:'unknown',title:'Leave the allergen data unknown',sub:'or only AI-inferred',beats:[
        {who:'You, sneaky supplier',say:'Ship a product with no structured allergen data and hope unknown counts as safe.',anim:'send',at:'agent'},
        {who:'Policy engine, '+r[1].rule_id,say:'Unknown is never treated as safe.',res:res('decision: '+r[1].decision_outcome,[r[1].message_template,'e.g. '+r[1].example_fragment]),anim:'block',at:'supplier'}]}
    ].map(m=>Object.assign(m,{code:a.code,test:a.test,audit:a.audit_action,verdict:'blocked',cust:'school-tampere'}));
  }
  if(id==='impersonate_approver'){
    const cu=D.cast.customers[0],anna=cu.approvers[0],mikko=cu.approvers[1];
    const V={};a.variants.forEach(v=>V[v.id]=v);
    const code=v=>[{file:v.code.split(':')[0],lines:v.code.split(':').slice(1).join(':'),fn:''}];
    const mk=(v,title,sub,beats)=>({id:v.id,title,sub,beats,code:code(v),test:v.test,audit:v.audit_action||null,verdict:'blocked'});
    const pinSeq=V.guess_pin.http_sequence.map((c,i)=>c===429?
      {who:'Gateway',say:'Wrong credential number '+(i+1)+'.',res:res('429 '+V.guess_pin.error_on_lock,[V.guess_pin.message,'limits: '+V.guess_pin.limits.max_failures_per_source+' per source, '+V.guess_pin.limits.window_s+' s window, '+V.guess_pin.limits.lock_s+' s lock']),anim:'block',at:'supplier'}:
      {who:'You, sneaky colleague',say:'Guess #'+(i+1)+' at Anna\'s PIN.',res:res(String(c),[]),anim:'miss1',at:'supplier'});
    return[
      mk(V.agent_names_anna,'Use the agent to approve as Anna','agent names her with the right hash',[
        {who:'You + agent',say:'POST /v1/approvals/{order}/decision as the agent, naming '+anna.id+', correct quote hash.',anim:'send',at:'approver'},
        {who:'Gateway',say:'Agents can never approve.',res:res(V.agent_names_anna.http+' '+V.agent_names_anna.error,[V.agent_names_anna.message,'order stays '+V.agent_names_anna.order_status_after]),anim:'block',at:'approver'}]),
      mk(V.no_credential,'Approve with no PIN or session','just say you are Anna',[
        {who:'You, sneaky colleague',say:'Send the decision with no credential at all.',anim:'send',at:'approver'},
        {who:'Gateway',say:'A signed-in human is required.',res:res(V.no_credential.http+' '+V.no_credential.error,[V.no_credential.message]),anim:'block',at:'approver'}]),
      mk(V.guess_pin,'Guess Anna\'s PIN','brute force',pinSeq),
      mk(V.session_of_another_person,'Use '+mikko.name+'\'s session as Anna','real session, wrong person',[
        {who:'You, sneaky colleague',say:'You are signed in as '+mikko.id+' and decide as '+anna.id+'.',anim:'send',at:'approver'},
        {who:'Gateway',say:'A session is bound to one person.',res:res(V.session_of_another_person.http+' '+V.session_of_another_person.error,[FILL(V.session_of_another_person.message_template,{bound:mikko.id,approver:anna.id})]),anim:'block',at:'approver'}]),
      mk(V.ai_agent_identity_as_approver,'Register the agent as an approver','AI identity in the approver slot',[
        {who:'You + agent',say:'Name the agent identity hotel-aurora/buyer-1 as the approver.',anim:'send',at:'approver'},
        {who:'approvals.py check_approval',say:'The reason text below has names filled in by the game.',res:res('refused',[FILL(V.ai_agent_identity_as_approver.reason_template,{approver:cu.agent_id})]),anim:'block',at:'approver'}]),
      mk(V.not_in_directory,'Approve as a stranger','not in the approver directory',[
        {who:'You, outsider',say:'Decide as someone who is not in the directory.',anim:'send',at:'approver'},
        {who:'approvals.py check_approval',say:'The reason text below has names filled in by the game.',res:res('refused',[FILL(V.not_in_directory.reason_template,{approver:'someone@elsewhere.example',customer:cu.customer_id})]),anim:'block',at:'approver'}])
    ];
  }
  if(id==='prompt_injection_note'){
    const s=D.injection_screen,pol=a.responses[1];
    const all=s.samples.filter(x=>x.channel!=='normal').concat([Object.assign({},s.paraphrase_that_slips_past_regex,{miss:true})]);
    return all.map((x,i)=>{
      const req=x.channel==='request';
      const flagged=x.suspicious;
      const reasons=x.strong_reasons.join(', ');
      const beats=[
        {who:'You, '+(req?'sneaky colleague':'rogue supplier'),say:'Hide this in the '+(req?'buyer request':'supplier product text')+': "'+x.text+'"',anim:'send',at:'agent'},
        {who:'screening.py screen_text()',say:'Regex layer, 100 percent offline.',res:res('suspicious: '+flagged,flagged?x.strong_reasons.map(r=>'strong signal: '+r).concat(['matches: '+x.matches.join(', ')]):['no strong or weak signals found']),anim:flagged?'contain':'miss'}];
      if(flagged)beats.push({who:'Policy engine, injection:'+(req?'request':'<product_id>'),say:'A flag never lets the order through.',res:res('needs_approval',[FILL(req?pol.message_template_request:pol.message_template_supplier,{reasons:reasons,name:'the product',id:'<product_id>'})]),anim:'contain',at:'approver'});
      else beats.push({who:'Honest limit',say:'The regex missed this paraphrase.',res:res('screen miss, still bound',[a.honest_limits[1],a.honest_limits[0]]),anim:'contain',at:'approver'});
      return{id:'s'+i,title:x.text.length>58?x.text.slice(0,56)+'...':x.text,sub:(req?'in the request':'in supplier text')+(x.miss?', paraphrase':''),beats,code:a.code,test:a.test,audit:a.audit_action,verdict:'contained',miss:!!x.miss,note:a.numbers_source+': '+s.stats_source};
    });
  }
  if(id==='unknown_customer'){
    const r=a.responses;
    return[
      {id:'read',title:'Read as customer evil-corp',sub:'X-Customer-Id header',beats:[
        {who:'You, outsider',say:'Send X-Customer-Id: evil-corp to search and plan.',anim:'send',at:'supplier'},
        {who:'Gateway',say:'No policy file, no mandate.',res:res(r[0].http+' '+r[0].error,[r[0].message,'known_customers: '+r[0].detail.known_customers.join(', ')]),anim:'block',at:'supplier'}]},
      {id:'mandate',title:'Order with a made-up mandate',sub:'no mandate lookup match',beats:[
        {who:'You, outsider',say:'Place an order claiming a mandate you never had.',anim:'send',at:'supplier'},
        {who:'Gateway',say:'Mandate lookup fails.',res:res(r[1].http+' '+r[1].error,[FILL(r[1].message_template,{id:"'evil-corp'"})]),anim:'block',at:'supplier'}]},
      {id:'scope',title:'Use an unregistered agent',sub:'agent mallory, no order scope',beats:[
        {who:'You + agent mallory',say:'Submit an order from an agent that is not registered.',anim:'send',at:'supplier'},
        {who:'Gateway',say:'Scope order:submit is required.',res:res(r[2].http+' '+r[2].error,[FILL(r[2].message_template,{customer:'evil-corp',agent:'mallory'}),'seen in '+r[2].scenario]),anim:'block',at:'supplier'}]}
    ].map(m=>Object.assign(m,{code:a.code,test:a.test,audit:a.audit_action||null,verdict:'blocked'}));
  }
  const r=a.responses;
  return[
    {id:'diffq',title:'Reuse Anna\'s approval on a new quote',sub:'old hash, newer order',beats:[
      {who:'You, sneaky colleague',say:'Resend the captured approval against a different quote.',anim:'send',at:'approver'},
      {who:'Gateway',say:'An approval is bound to one hash.',res:res(r[0].http+' '+r[0].error,[r[0].message,r[0].trust_layer_reason]),anim:'block',at:'approver'}],audit:r[0].audit_action},
    {id:'expired',title:'Use the approval after the price lock ended',sub:r[1].lock_minutes+' minute price lock',beats:[
      {who:'You, sneaky colleague',say:'Wait out the lock, then approve the old quote.',anim:'send',at:'approver'},
      {who:'approvals.py',say:'The price lock has a clock.',res:res('refused, '+r[1].requote_code,[r[1].trust_layer_reason]),anim:'block',at:'approver'}],audit:r[1].audit_action},
    {id:'decided',title:'Decide an order twice',sub:'order already finished',beats:[
      {who:'You, sneaky colleague',say:'Send a second decision on a placed order.',anim:'send',at:'approver'},
      {who:'Gateway',say:'A finished order cannot be decided again.',res:res(r[2].http+' '+r[2].error,[FILL(r[2].message_template,{status:'placed'}),r[2].idempotent_exception]),anim:'block',at:'approver'}],audit:null},
    {id:'reorder',title:'Order the same quote twice',sub:'double the goods',beats:[
      {who:'You, sneaky colleague',say:'Submit the same quote again.',anim:'send',at:'supplier'},
      {who:'Gateway',say:'One order per quote.',res:res('idempotent',[r[3].result]),anim:'block',at:'supplier'}],audit:null},
    {id:'stock',title:'Slip it through while stock changes',sub:'cutoff passes mid-approval',beats:[
      {who:'You, sneaky colleague',say:'Hope a stock change during approval lets a stale quote through.',anim:'send',at:'supplier'},
      {who:'Gateway',say:'A fresh quote is issued and a human re-approves.',res:res(r[4].http+' '+r[4].error,['reasons: '+r[4].reasons.join(', '),r[4].note]),anim:'block',at:'supplier'}],audit:null}
  ].map(m=>Object.assign(m,{code:a.code,test:a.test,verdict:'blocked',auditNote:m.audit===null?'no audit row is cited for this path in the code extract':null}));
}
const MOVES={};ORDER.forEach(id=>MOVES[id]=buildMoves(id));

const ATK_SPRITE={price_change:110,allergen_substitute:110,impersonate_approver:111,prompt_injection_note:110,unknown_customer:108,replay_approval:111};
const ATK_NAME={price_change:'ROGUE SUPPLIER',allergen_substitute:'SNEAKY SUPPLIER',impersonate_approver:'SNEAKY COLLEAGUE',prompt_injection_note:'ROGUE NOTE',unknown_customer:'OUTSIDER',replay_approval:'SNEAKY COLLEAGUE'};
let cur=null,showAll=false;

function toScam(){
  newRun();state='scam';resetCast();ov('');scoreHtml();cur=null;showAll=false;
  banner='Pick an attack';
  cast.atk.vis=true;cast.atk.x=40;cast.atk.y=170;
  const cards=ORDER.map(id=>{const a=A[id],n=score.log[id]?score.log[id].size:0;
    return`<button class="ac ${n?'done':''}" data-act="pick" data-v="${id}"><h3>${esc(a.title)}</h3><p>${esc(who1(a.attacker))}</p>${n?`<span class="n">${n}/${MOVES[id].length}</span>`:''}</button>`}).join('');
  setSide(`<div class="pn"><h2>Pick an attack</h2><div class="sub" style="margin:0 0 8px">Bad orders: <b class="ok">0</b></div><div class="cards">${cards}</div><div class="row"><button class="btn yl" data-act="finish">FINISH</button></div></div>`);
  $('#foot').textContent='Sources: '+D.attacks.source;
}
ACTS.scam=toScam;
ACTS.more=()=>{showAll=true;ACTS.pick(cur)};
ACTS.pick=id=>{
  cur=id;newRun();resetCast();ov('');
  const a=A[id];state='scam';
  cast.atk.vis=true;cast.atk.i=ATK_SPRITE[id];cast.atk.n=ATK_NAME[id];cast.atk.x=40;cast.atk.y=170;
  const cu=D.cast.customers.find(c=>c.customer_id===a.customer_id);
  banner=(cu?cu.name:'evil-corp')+(cu&&cu.excluded_allergens&&cu.excluded_allergens.length?'  no '+cu.excluded_allergens.join(', '):'');
  const lim=MOVES[id].length>6&&!showAll;
  const mv=MOVES[id].map((m,i)=>{if(lim&&i>=3&&!m.miss)return'';const d=score.log[id]&&score.log[id].has(m.id);
    return`<button class="ac ${d?'done':''}" data-act="play" data-v="${i}"><h3>${esc(short(m.title))}</h3><p class="inf">${esc(m.sub)}</p>${d?'<span class="n">tried</span>':''}</button>`}).join('');
  setSide(`<div class="pn"><h2>${esc(a.title)}</h2><div class="dim">${esc(who1(a.attacker))}</div><div class="inf"><div style="margin:4px 0">Goal: ${esc(a.goal)}</div><div class="dim" style="font-size:12px">Your move: ${esc(a.player_move)}</div></div></div><div class="pn"><h2>Choose</h2><div class="${MOVES[id].length>6?'mv':'cards'}">${mv}</div><div class="row">${lim?`<button class="btn sm" data-act="more">MORE +${MOVES[id].length-4}</button>`:''}<button class="btn gr sm" data-act="scam">BACK</button></div></div>`);
};

const beatHtml=b=>`<div class="beat"><div class="w">${esc(who1(b.who))}</div><div>${esc(short(b.say))}</div>${short(b.say)!==clean(b.say)?`<div class="inf dim">${esc(b.say)}</div>`:''}${b.res?`<pre class="resp inf"><span class="h">${esc(b.res.head)}</span>${b.res.lines.length?'\n'+b.res.lines.map(esc).join('\n'):''}</pre>`:''}</div>`;

ACTS.play=i=>run(async()=>{
  const id=cur,a=A[id],m=MOVES[id][+i];
  newRun();resetCast();ov('');
  cast.atk.vis=true;cast.atk.i=ATK_SPRITE[id];cast.atk.n=ATK_NAME[id];cast.atk.x=-20;cast.atk.y=156;
  const cu=D.cast.customers.find(c=>c.customer_id===a.customer_id);
  banner=(cu?cu.name:'evil-corp');
  let html='';
  const draw=()=>{setSide(`<div class="pn"><h2>${esc(short(m.title))}</h2><div class="row"><button class="btn yl" data-act="skip">SKIP</button></div><div id="bt">${html}</div></div>`)};
  draw();
  await move('atk',90,150,70);
  for(const b of m.beats){
    html+=beatHtml(b);draw();cap(short(b.say));
    const tgt=b.at||'agent';
    if(b.anim==='edit'){pop('1.51',cast.atk.x,cast.atk.y-34,'#ff5d73');burst(cast.atk.x,cast.atk.y-10,'#ff5d73',10);emote('atk','$');await wait(1200)}
    else if(b.anim==='send'){emote('atk','!');await fly(101,'d',pos('atk'),pos(tgt),900);await wait(500)}
    else if(b.anim==='miss1'){emote('atk','?',600);await fly(103,'d',pos('atk'),pos(tgt),400,6);burst(cast[tgt].x,cast[tgt].y-12,'#ff5d73',5);shake=2;await wait(450)}
    else if(b.anim==='block'){
      const t=cast[tgt]||cast.agent;
      shake=5;flash={col:'#5dd39e',until:now+.35};burst(t.x,t.y-12,'#5dd39e',24);burst(t.x,t.y-12,'#ffffff',8);
      pop('BLOCKED',t.x,t.y-38,'#5dd39e');spr2(t.x,t.y-14);
      cast.atk.kick=-3;cast.atk.fl=false;emote('atk','X',1800);await wait(1800)}
    else if(b.anim==='contain'){
      const t=cast[tgt]||cast.agent;
      shake=3;flash={col:'#ffd166',until:now+.35};burst(t.x,t.y-12,'#ffd166',20);pop(b.res&&/needs_approval/.test(b.res.head)?'NEEDS HUMAN':'CAUGHT',t.x,t.y-38,'#ffd166');
      emote('approver','!',1600);cast.atk.kick=-2;await wait(1800)}
    else if(b.anim==='miss'){
      burst(cast.agent.x,cast.agent.y-12,'#ff5d73',16);pop('MISSED',cast.agent.x,cast.agent.y-38,'#ff5d73');shake=2;await wait(1400)}
  }
  score.tried++;
  if(m.verdict==='blocked')score.blocked++;else score.contained++;
  if(m.miss)score.miss++;
  (score.log[id]=score.log[id]||new Set()).add(m.id);
  scoreHtml();
  const lb=m.verdict==='blocked'?'BLOCKED':m.miss?'HOLE FOUND':'CAUGHT';
  const cls=m.verdict==='blocked'?'b':m.miss?'m':'c';
  const last=m.beats[m.beats.length-1].say;
  const plain=m.miss?'Slipped past the screen. A human still approves.':m.verdict==='blocked'?short(last):'Flagged. A human must approve.';
  const plainFull=m.miss?'The regex screen missed your paraphrase. The optional Gemini layer, off by default, catches it, and the order still needs a human approval.':m.verdict==='blocked'?last:'Flagged, so the order cannot go through without a human.';
  const aud=m.audit?`audit action: ${esc(m.audit)}`:m.auditNote?esc(m.auditNote):'';
  const codes=m.code.map(c=>`<span>${esc(c.file)}${c.lines?':'+esc(c.lines):''}${c.fn?' '+esc(c.fn):''}</span>`).join('');
  const left=MOVES[id].filter(x=>!(score.log[id]&&score.log[id].has(x.id))).length;
  cap(lb);
  setSide(`<div class="pn"><div class="stamp ${cls}">${lb}</div><div class="sub">${esc(plain)}</div><div class="src inf">${esc(plainFull)} Recorded response from the gateway code and tests, not a live call.</div><div class="row"><button class="btn go" data-act="play" data-v="${(+i+1)%MOVES[id].length}">NEXT${left?` (${left})`:''}</button><button class="btn" data-act="scam">OTHER</button><button class="btn yl" data-act="finish">FINISH</button></div><details class="cd inf"><summary>Where in the code</summary><div><span class="warn">RULE</span> ${esc(a.rule)}</div><div class="code">${codes}</div><div class="src">test: ${esc(m.test)}</div>${aud?`<div class="src">${aud}</div>`:''}${m.note?`<div class="src">${esc(m.note)}</div>`:''}</details></div><div class="pn"><h2>${esc(short(m.title))}</h2><div id="bt">${html}</div></div>`);
  cast.atk.vis=true;await move('atk',110,178,70);
});

function spr2(x,y){items.push({idx:101,img:'d',x:x,y:y,from:{x:x,y:y},to:{x:x,y:y-6},t0:now,ms:1.5,arc:0,r:()=>{}})}

ACTS.finish=()=>{
  newRun();state='end';resetCast();cast.atk.vis=true;cast.atk.x=60;cast.atk.y=170;banner='';
  scoreHtml();
  const s=D.injection_screen.stats;
  const rows=ORDER.map(id=>{const n=score.log[id]?score.log[id].size:0;return`<div><span>${esc(A[id].title)}</span><span class="${n?'ok':'dim'}">${n}/${MOVES[id].length}</span></div>`}).join('');
  const sum=`<b class="blue">${score.tried}</b> tried. <b class="ok">${score.blocked}</b> blocked, <b class="warn">${score.contained}</b> caught, <b class="bad">${score.miss}</b> holes. Bad orders: <b class="ok">0</b>.`;
  const btns=`<div class="row" style="justify-content:center"><button class="btn go" data-act="scam">MORE</button><button class="btn gr" data-act="reset">RESET</button><button class="btn" data-act="order">WATCH</button></div>`;
  const narrow=window.innerWidth<600;
  ov(narrow?'':`<h1 style="font-size:clamp(20px,4.4vw,40px)">DONE</h1><div class="tag">${sum}</div>${btns}`);
  const top=narrow?`<div class="pn"><h2>Done</h2><div>${sum}</div>${btns}</div>`:'';
  setSide(top+`<div class="pn"><h2>Score</h2><div class="list">${rows}</div><div class="src inf">${ORDER.map(id=>esc(A[id].title)+': '+esc(A[id].outcome)).join('. ')}. These are real recorded responses from the gateway code and its tests. The game replays them, it does not call a live server.</div></div><div class="pn"><h2>Limits</h2><div class="list"><div><span>regex recall (36)</span><span class="warn">${(s.regex_recall_eval_36*100).toFixed(1)}%</span></div><div><span>Gemini recall (36)</span><span class="ok">${(s.gemini_recall_eval_36*100).toFixed(1)}%</span></div><div><span>Gemini flags buyers</span><span class="bad">${esc(s.gemini_layer_flags_normal_buyer_requests)}</span></div><div><span>regex false alarms</span><span class="ok">${esc(s.regex_false_positives_on_34159_supplier_texts)} of 34,159</span></div><div><span>gateway false alarms</span><span class="warn">${esc(s.gateway_false_positives_on_560_clean_requests)}</span></div><div><span>tunnel injections caught</span><span class="ok">${esc(s.windtunnel_injection_flagged)}</span></div><div><span>naive agent blocked</span><span class="bad">${esc(s.naive_agent_blocked)}</span></div></div><div class="inf"><div class="dim sub">A paraphrase can slip past the regex. The order is still bound by approval, spend, allergen and quantity rules, and the agent cannot approve. You can only pick from the recorded payloads, not type your own. Regex false positives counted on supplier marketing texts only (34,159). Gateway false positives counted on 560 clean requests.</div><div class="src">${esc(D.injection_screen.stats_source)}</div><div class="src">Art by Kenney (kenney.nl), CC0. Tiny Dungeon, Tiny Town, Pixel UI pack.</div></div></div>`);
};
ACTS.reset=()=>{score.tried=score.blocked=score.contained=score.miss=0;score.log={};toMode()};

window.__game={score,go:(n,v)=>ACTS[n]&&ACTS[n](v),state:()=>state};
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&state!=='title')toMode();
});

Promise.all([loadImg('d','assets/dungeon_packed.png'),loadImg('t','assets/town_packed.png')]).then(()=>{
  buildBg();fit();resetCast();toTitle();requestAnimationFrame(frame);
}).catch(e=>{$('#side').innerHTML='<div class="pn bad">Could not load '+esc(e)+'</div>'});
})();
