const state={workflow:'scenes',selectionMode:'tiles',aoi:null,plan:null,tasks:[],directory:'',directoryPurpose:'output',catalog:null,capabilities:null,mgrsVisible:true,mgrsCoverage:null};
const queryToken=new URLSearchParams(location.search).get('token');
if(queryToken)localStorage.setItem('s1grits_web_token',queryToken);
const apiToken=queryToken||localStorage.getItem('s1grits_web_token')||'';
let map=null,aoiLayer=null,resolvedGridLayer=null,mgrsGridLayer=null,mgrsRenderer=null,roadLayer=null,satelliteLayer=null,baseMode='road',tileErrorCount=0;
let mgrsRequest=null,mgrsRequestSerial=0,mgrsLoadTimer=null,mgrsPermanentLabels=false,mapNoticeTimer=null;
let activeLog=null,logPollTimer=null;
const MGRS_VIEW_PADDING=.2;
const $=id=>document.getElementById(id);
const escapeHtml=value=>String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

async function api(path,options={}){
  const auth=apiToken?{'Authorization':`Bearer ${apiToken}`}:{},response=await fetch(path,{headers:{'Content-Type':'application/json',...auth,...(options.headers||{})},...options});
  let body={}; try{body=await response.json()}catch{body={error:await response.text()}}
  if(!response.ok) throw new Error(body.error||body.detail||`请求失败 (${response.status})`);
  return body;
}

function buildTags(){
  const current=state.capabilities?.current_year||new Date().getFullYear();
  $('years').innerHTML=Array.from({length:current-2013},(_,i)=>2014+i).map(year=>`<label><input type="checkbox" value="${year}" ${year>=current-1?'checked':''}><span>${year}</span></label>`).join('');
  $('months').innerHTML=Array.from({length:12},(_,i)=>`<label><input type="checkbox" value="${i+1}" checked><span>${i+1}</span></label>`).join('');
}

function setWorkflow(workflow){
  state.workflow=workflow;
  document.querySelectorAll('[data-workflow]').forEach(el=>el.classList.toggle('active',el.dataset.workflow===workflow));
  $('time-pane').hidden=false;
  $('static-options').hidden=!$('include-static').checked;
  $('smonthly').parentElement.style.display=workflow==='scenes'?'flex':'none';
  if(workflow!=='scenes') $('smonthly').checked=false;
}

function setSelection(mode){
  state.selectionMode=mode;
  document.querySelectorAll('[data-mode]').forEach(el=>el.classList.toggle('active',el.dataset.mode===mode));
  $('tile-pane').hidden=mode!=='tiles'; $('aoi-pane').hidden=mode!=='aoi';
}

function polygonGeometry(value){
  if(!value) return null;
  if(value.type==='Feature') return polygonGeometry(value.geometry);
  if(value.type==='FeatureCollection'){
    const polygons=value.features.map(polygonGeometry).filter(Boolean);
    const coordinates=[];
    polygons.forEach(g=>{if(g.type==='Polygon') coordinates.push(g.coordinates); else coordinates.push(...g.coordinates)});
    return {type:'MultiPolygon',coordinates};
  }
  if(value.type==='Polygon'||value.type==='MultiPolygon') return value;
  throw new Error('仅支持 Polygon 或 MultiPolygon AOI');
}

function polygons(geometry){
  if(!geometry) return [];
  return geometry.type==='Polygon'?[geometry.coordinates]:geometry.coordinates;
}

function svgPath(geometry){
  return polygons(geometry).map(poly=>poly.map(ring=>`M${ring.map(p=>`${Number(p[0]).toFixed(4)},${Number(p[1]).toFixed(4)}`).join(' L')} Z`).join(' ')).join(' ');
}

function showMapError(message,autoHideMs=0){
  const box=$('map-error');clearTimeout(mapNoticeTimer);box.textContent=message;box.hidden=false;
  if(autoHideMs>0)mapNoticeTimer=setTimeout(()=>{box.hidden=true},autoHideMs);
}

function setMgrsWarning(message=''){
  const button=$('toggle-mgrs');button.classList.toggle('warning',Boolean(message));
  button.title=message||'缩放至4级后显示MGRS格网';
}

function setMgrsStatus(message,isError=false){if(isError){setMgrsWarning(message);showMapError(message,4500)}}

function expandedViewport(){
  const bounds=map.getBounds(),west=Math.max(-180,bounds.getWest()),east=Math.min(180,bounds.getEast()),south=Math.max(-85.051129,bounds.getSouth()),north=Math.min(85.051129,bounds.getNorth());
  const dx=(east-west)*MGRS_VIEW_PADDING,dy=(north-south)*MGRS_VIEW_PADDING;
  return {west:Math.max(-180,west-dx),south:Math.max(-85.051129,south-dy),east:Math.min(180,east+dx),north:Math.min(85.051129,north+dy)};
}

function viewportCovered(coverage){
  if(!coverage||coverage.zoom!==map.getZoom())return false;
  const bounds=map.getBounds();
  return bounds.getWest()>=coverage.west&&bounds.getEast()<=coverage.east&&bounds.getSouth()>=coverage.south&&bounds.getNorth()<=coverage.north;
}

function addMgrsTile(tileId){
  const values=$('tiles').value.split(/[\s,;]+/).map(value=>value.trim().toUpperCase()).filter(Boolean);
  if(!values.includes(tileId))values.push(tileId);
  $('tiles').value=values.join('\n');setSelection('tiles');
  setMgrsStatus(`已将 ${tileId} 加入瓦片输入`);if(map)map.closePopup();
}

function bindMgrsFeature(feature,layer){
  const tileId=feature.properties.tile_id,epsg=feature.properties.utm_epsg;
  layer.bindTooltip(tileId,{sticky:!mgrsPermanentLabels,permanent:mgrsPermanentLabels,direction:'center',className:'mgrs-label'});
  layer.bindPopup(`<b>${escapeHtml(tileId)}</b><br>生产投影：EPSG:${escapeHtml(epsg)}<br><button type="button" class="mgrs-add" onclick="addMgrsTile('${escapeHtml(tileId)}')">加入瓦片输入</button>`);
}

async function loadMgrsGrid(force=false){
  if(!map||!mgrsGridLayer)return;
  if(!state.mgrsVisible){mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning();return}
  const minZoom=state.capabilities?.mgrs_map?.min_zoom??4,zoom=map.getZoom();
  if(zoom<minZoom){if(mgrsRequest)mgrsRequest.abort();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning(`放大至 ${minZoom} 级显示MGRS格网`);return}
  if(!force&&viewportCovered(state.mgrsCoverage))return;
  const coverage=expandedViewport(),bbox=[coverage.west,coverage.south,coverage.east,coverage.north].map(value=>value.toFixed(6)).join(',');
  if(mgrsRequest)mgrsRequest.abort();mgrsRequest=new AbortController();const serial=++mgrsRequestSerial;
  setMgrsStatus('正在加载MGRS格网…');
  try{
    const data=await api(`/api/map/mgrs?bbox=${encodeURIComponent(bbox)}&zoom=${zoom}`,{signal:mgrsRequest.signal});
    if(serial!==mgrsRequestSerial)return;
    if(data.truncated){mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsStatus(`当前视窗包含 ${data.count} 个格网，请继续放大`,true);return}
    setMgrsWarning();mgrsPermanentLabels=zoom>=7&&data.returned<=300;mgrsGridLayer.clearLayers();mgrsGridLayer.addData(data.features);
    state.mgrsCoverage={...coverage,zoom};setMgrsStatus(`MGRS参考格网 ${data.returned} 个 · z${zoom}`);
  }catch(error){
    if(error.name==='AbortError')return;
    state.mgrsCoverage=null;setMgrsStatus(`MGRS参考格网加载失败：${error.message}`,true);
  }
}

function scheduleMgrsLoad(force=false){
  clearTimeout(mgrsLoadTimer);mgrsLoadTimer=setTimeout(()=>loadMgrsGrid(force),180);
}

function initMap(){
  if(typeof L==='undefined'){showMapError('项目内置 Leaflet 资源未加载，请重新安装 S1-GRiTS Web 组件并重启服务。');return}
  map=L.map('map',{center:[34.5,105],zoom:4,zoomControl:true,worldCopyJump:false,preferCanvas:true,maxBounds:[[-85.051129,-180],[85.051129,180]],maxBoundsViscosity:1});
  map.createPane('mgrsReferencePane');map.getPane('mgrsReferencePane').style.zIndex=410;
  map.createPane('aoiPane');map.getPane('aoiPane').style.zIndex=440;
  map.createPane('resolvedGridPane');map.getPane('resolvedGridPane').style.zIndex=450;
  roadLayer=L.tileLayer('https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',{maxZoom:19,subdomains:['mt0','mt1','mt2','mt3'],attribution:'&copy; Google',keepBuffer:2,noWrap:true});
  satelliteLayer=L.tileLayer('https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',{maxZoom:19,subdomains:['mt0','mt1','mt2','mt3'],attribution:'Imagery &copy; Google',keepBuffer:2,noWrap:true});
  [roadLayer,satelliteLayer].forEach(layer=>layer.on('tileerror',()=>{tileErrorCount+=1;if(tileErrorCount===4)showMapError('在线底图暂时无法访问；MGRS格网、AOI与服务端相交计算仍可正常使用。')}));
  mgrsRenderer=L.canvas({pane:'mgrsReferencePane',padding:.5});
  mgrsGridLayer=L.geoJSON(null,{renderer:mgrsRenderer,pane:'mgrsReferencePane',style:{color:'#49675d',weight:.7,opacity:.62,fill:false,interactive:true},onEachFeature:bindMgrsFeature}).addTo(map);
  roadLayer.addTo(map);aoiLayer=L.layerGroup().addTo(map);resolvedGridLayer=L.layerGroup().addTo(map);
  map.on('moveend zoomend',()=>scheduleMgrsLoad());scheduleMgrsLoad(true);
  window.addEventListener('resize',()=>map.invalidateSize());setTimeout(()=>map.invalidateSize(),100);
}

function setBaseMap(mode){
  baseMode=mode;if(!map)return;
  [roadLayer,satelliteLayer].forEach(layer=>{if(layer&&map.hasLayer(layer))map.removeLayer(layer)});
  if(mode==='road')roadLayer.addTo(map);else if(mode==='satellite')satelliteLayer.addTo(map);
  document.querySelectorAll('[data-basemap]').forEach(el=>el.classList.toggle('active',el.dataset.basemap===mode));
}

function toggleMgrs(){
  state.mgrsVisible=!state.mgrsVisible;const button=$('toggle-mgrs');button.classList.toggle('active',state.mgrsVisible);button.setAttribute('aria-pressed',String(state.mgrsVisible));
  if(state.mgrsVisible){if(!map.hasLayer(mgrsGridLayer))mgrsGridLayer.addTo(map);scheduleMgrsLoad(true)}else{if(mgrsRequest)mgrsRequest.abort();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning()}
}

function drawAOI(){
  if(!map||!aoiLayer)return;aoiLayer.clearLayers();
  if(state.aoi){const layer=L.geoJSON(state.aoi,{pane:'aoiPane',style:{color:'#c8781b',weight:2,dashArray:'7 5',fillColor:'#e49b33',fillOpacity:.16}}).addTo(aoiLayer);map.fitBounds(layer.getBounds(),{padding:[25,25],maxZoom:9})}
}

function drawPlan(plan){
  if(map&&resolvedGridLayer){resolvedGridLayer.clearLayers();const collection={type:'FeatureCollection',features:plan.tile_features||[]};const layer=L.geoJSON(collection,{pane:'resolvedGridPane',style:{color:'#176b51',weight:1.5,fillColor:'#23926e',fillOpacity:.23},onEachFeature:(feature,item)=>item.bindPopup(`<b>${escapeHtml(feature.properties.tile_id)}</b><br>EPSG:${feature.properties.utm_epsg}`)}).addTo(resolvedGridLayer);if(layer.getLayers().length)map.fitBounds(layer.getBounds(),{padding:[28,28],maxZoom:9})}
}

function applyBBox(){
  const w=Number($('west').value),e=Number($('east').value),s=Number($('south').value),n=Number($('north').value);
  if(![w,e,s,n].every(Number.isFinite)||w>=e||s>=n||w<70||e>140||s<10||n>55){
    $('aoi-status').textContent='请输入中国区域内有效的西、东、南、北边界。'; return;
  }
  state.aoi={type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]};
  $('aoi-status').textContent=`矩形 AOI：${w}, ${s} — ${e}, ${n}（EPSG:4326）`; drawAOI();
}

function selected(selector){return [...document.querySelectorAll(selector+':checked')].map(el=>Number(el.value))}
function payload(){
  return {
    workflow:state.workflow,selection_mode:state.selectionMode,
    tiles:$('tiles').value,aoi:state.aoi,direction:$('direction').value,
    years:selected('#years input'),months:selected('#months input'),output_subdir:$('output').value,
    zarr_only:$('zarr-only').checked,include_static:$('include-static').checked,smonthly:$('smonthly').checked,
    target_resolution:Number($('resolution').value),max_workers:Number($('workers').value),
    spatial_despeckle:$('despeckle').checked,features_ratio:$('ratio').checked,features_rvi:$('rvi').checked,
    static_layers:[...document.querySelectorAll('[name=static-layer]:checked')].map(el=>el.value)
  };
}

async function preflight(){
  const button=$('submit'); button.disabled=true; $('form-message').textContent='正在执行空间、容量与配置预检…';
  try{
    const plan=await api('/api/plan',{method:'POST',body:JSON.stringify(payload())}); state.plan=plan; drawPlan(plan);
    const temporal=`${plan.years.join('、')} 年 · ${plan.months.length} 个月份`,products=plan.include_static?`${plan.workflow} + static`:plan.workflow;
    $('plan-summary').innerHTML=`<dl><dt>产品</dt><dd>${escapeHtml(products)}</dd><dt>瓦片</dt><dd>${plan.tiles.length} 个：${escapeHtml(plan.tiles.slice(0,18).join(', '))}${plan.tiles.length>18?' …':''}</dd><dt>轨道</dt><dd>${escapeHtml(plan.directions.join(' → '))}</dd><dt>时间</dt><dd>${escapeHtml(temporal)}</dd><dt>输出</dt><dd><code>${escapeHtml(plan.output_dir)}</code></dd><dt>规划估算</dt><dd>${plan.raw_gib.toFixed(3)} GiB</dd></dl><p class="hint">${escapeHtml(plan.estimate_note)}</p>`;
    const needs=Boolean(plan.confirmation_phrase); $('phrase-wrap').hidden=!needs; $('phrase').value=''; $('phrase').placeholder=needs?plan.confirmation_phrase:'';
    $('confirm-dialog').showModal(); $('form-message').textContent='预检通过，请核对规划。';
  }catch(error){$('form-message').textContent=error.message}
  finally{button.disabled=false}
}

async function confirmTask(event){
  event.preventDefault(); if(!state.plan)return;
  const button=$('confirm-run'); button.disabled=true;
  try{
    const task=await api('/api/tasks',{method:'POST',body:JSON.stringify({plan_id:state.plan.plan_id,confirmation:$('phrase').value})});
    $('confirm-dialog').close(); state.plan=null; $('form-message').textContent=`任务 ${task.run_id} 已进入队列。`; await loadTasks();
  }catch(error){$('form-message').textContent=error.message}
  finally{button.disabled=false}
}

const statusNames={queued:'排队中',running:'处理中',processing:'影像处理',static:'静态图层',validating:'目录复验',associating:'关联复验',done:'已完成',failed:'失败',cancelled:'已取消',cancelling:'取消中',interrupted:'已中断'};
function taskCard(task){
  const progress=Math.max(0,Math.min(1,Number(task.progress)||0));
  const temporal=`${(task.years||[]).join(',')} · ${(task.months||[]).length}月`;
  const productLabel=task.include_static?`${task.workflow} + static`:task.workflow;
  const action=!['done','failed','cancelled','interrupted'].includes(task.status)?`<button class="secondary" onclick="cancelTask('${task.run_id}')">取消</button>`:'';
  const association=task.validation?.static_association?` · Static关联 ${task.validation.static_association.pairs_checked}`:'';
  const validation=task.validation?`<div class="task-meta ok">Catalog ${task.validation.records} 条 · ${task.validation.tiles} 瓦片${association}</div>`:'';
  const error=task.error?`<div class="task-meta error" title="${escapeHtml(task.error)}">${escapeHtml(task.error)}</div>`:'';
  const catalogArgument=escapeHtml(JSON.stringify(task.output_subdir||''));
  return `<article class="task-card"><div class="task-title"><b>${escapeHtml(productLabel)} · ${(task.tiles||[]).length} 瓦片</b><span class="badge ${escapeHtml(task.status)}">${statusNames[task.stage]||statusNames[task.status]||escapeHtml(task.status)}</span></div><div class="task-meta">${escapeHtml((task.directions||[]).join(' + '))} · ${escapeHtml(temporal)}</div><div class="task-meta" title="${escapeHtml(task.output_dir)}">${escapeHtml(task.output_subdir)}</div><div class="progress"><i style="width:${progress*100}%"></i></div>${validation}${error}<div class="task-actions"><button class="secondary" onclick="openLog('${task.run_id}')">日志</button>${action}<button class="secondary" onclick="catalogFor(${catalogArgument})">检索结果</button></div></article>`;
}

async function loadTasks(){
  try{state.tasks=await api('/api/tasks'); $('tasks').innerHTML=state.tasks.length?state.tasks.map(taskCard).join(''):''}catch(error){$('tasks').innerHTML=`<div class="empty error">${escapeHtml(error.message)}</div>`}
}
async function cancelTask(id){if(!confirm('确认取消该任务及其子进程？'))return; try{await api(`/api/tasks/${id}`,{method:'DELETE'});await loadTasks()}catch(error){alert(error.message)}}
function formatBytes(value){const bytes=Number(value)||0;if(bytes<1024)return `${bytes} B`;if(bytes<1024**2)return `${(bytes/1024).toFixed(1)} KiB`;return `${(bytes/1024**2).toFixed(1)} MiB`}

function renderTaskStages(task){
  const stages=[['queued','排队'],['processing','影像']];
  if(task.include_static)stages.push(['static','静态层']);
  stages.push(['validating','Catalog']);if(task.include_static)stages.push(['associating','关联']);stages.push(['done','完成']);
  const current=stages.findIndex(([key])=>key===task.stage),terminal=['failed','cancelled','interrupted'].includes(task.status);
  $('task-stage-track').innerHTML=stages.map(([key,label],index)=>{
    let cls='';if(task.status==='done'||index<current)cls='done';else if(index===current)cls=terminal?'failed':'active';
    return `<span class="stage-pill ${cls}">${escapeHtml(label)}</span>`;
  }).join('')+(terminal?`<span class="stage-pill failed">${escapeHtml(statusNames[task.status]||task.status)}</span>`:'');
}

function appendTaskEvents(events,reset=false){
  const box=$('task-events');if(reset)box.innerHTML='';
  if(events.length)box.insertAdjacentHTML('beforeend',events.map(event=>`<div class="task-event ${escapeHtml(event.level||'info')}"><time>${escapeHtml(event.timestamp||'')}</time><b>${escapeHtml(statusNames[event.stage]||event.event||'事件')}</b>${event.message?`<div>${escapeHtml(event.message)}</div>`:''}</div>`).join(''));
  while(box.children.length>200)box.firstElementChild.remove();
  if($('log-follow').checked)box.scrollTop=box.scrollHeight;
}

async function pollTaskLog(reset=false){
  if(!activeLog)return;const session=activeLog,id=session.id;
  if(reset){session.logOffset=0;session.eventOffset=0;$('task-log-output').textContent='';$('task-events').innerHTML=''}
  try{
    const tail=reset?'&tail=1':'';
    const [task,logChunk,eventChunk]=await Promise.all([
      api(`/api/tasks/${encodeURIComponent(id)}`),
      api(`/api/tasks/${encodeURIComponent(id)}/log?format=json&offset=${session.logOffset}&limit=65536${tail}`),
      api(`/api/tasks/${encodeURIComponent(id)}/events?offset=${session.eventOffset}&limit=65536`),
    ]);
    if(activeLog!==session)return;
    session.logOffset=logChunk.next_offset;session.eventOffset=eventChunk.next_offset;
    const output=$('task-log-output');if(logChunk.text)output.textContent=(output.textContent+logChunk.text).slice(-250000);
    appendTaskEvents(eventChunk.events,reset);if($('log-follow').checked)output.scrollTop=output.scrollHeight;
    $('task-log-title').textContent=`任务日志 · ${id}`;
    $('task-log-meta').textContent=`${statusNames[task.stage]||task.stage} · ${task.command_index||0}/${task.command_total||0} 条命令 · ${task.output_subdir}`;
    $('log-size').textContent=formatBytes(logChunk.size);renderTaskStages(task);
    clearTimeout(logPollTimer);if(!['done','failed','cancelled','interrupted'].includes(task.status))logPollTimer=setTimeout(()=>pollTaskLog(false),1500);
  }catch(error){if(activeLog===session)$('task-log-output').textContent+=`\n[日志读取失败] ${error.message}\n`}
}

function openLog(id){
  clearTimeout(logPollTimer);activeLog={id,logOffset:0,eventOffset:0};
  $('task-log-title').textContent=`任务日志 · ${id}`;$('task-log-meta').textContent='正在读取任务状态…';$('download-log').href=`/api/tasks/${encodeURIComponent(id)}/log`;
  if(!$('task-log-dialog').open)$('task-log-dialog').showModal();pollTaskLog(true);
}

function closeTaskLog(){clearTimeout(logPollTimer);activeLog=null;$('task-log-dialog').close()}
function catalogFor(output){openCatalogDialog(output,true)}

async function browseDirectory(path='',purpose=state.directoryPurpose,silent=false){
  try{
    const params=new URLSearchParams({path,mode:purpose==='catalog'?'catalog':'output'}),data=await api('/api/output-directories?'+params);state.directory=data.path;state.directoryPurpose=purpose;
    $('directory-dialog-title').textContent=purpose==='catalog'?'选择数据立方体目录':'选择输出目录';
    $('directory-dialog-hint').firstChild.textContent=purpose==='catalog'?'仅显示服务器输出根内的目录。绿色标记表示存在根级 catalog.parquet。服务器输出根：':'服务器输出根：';
    $('output-root').textContent=data.root;$('dir-current').textContent=data.path||'/';$('dir-up').disabled=data.parent===null;$('dir-up').dataset.parent=data.parent??'';
    $('new-folder-row').hidden=purpose==='catalog';$('select-output').textContent=purpose==='catalog'?'打开此 Catalog':'选择当前目录';$('select-output').disabled=purpose==='catalog'&&(!data.path||!data.catalog_available);
    const schema=state.capabilities?.catalog_schema_version??8;
    $('dir-list').innerHTML=data.directories.length?data.directories.map(item=>`<button data-path="${escapeHtml(item.path)}">📁 ${escapeHtml(item.name)}${purpose==='catalog'?(item.catalog_available?`<span class="catalog-ready">Catalog v${schema} 候选</span>`:'<span class="catalog-missing">无根级 Catalog</span>'):''}</button>`).join(''):'<div class="empty">这里还没有子目录</div>';
    document.querySelectorAll('#dir-list button').forEach(el=>el.onclick=()=>browseDirectory(el.dataset.path,purpose));return true;
  }catch(error){if(!silent)alert(error.message);return false}
}

async function openDirectoryBrowser(purpose,initial=''){
  state.directoryPurpose=purpose;let opened=await browseDirectory(initial,purpose,true);
  if(!opened&&initial)opened=await browseDirectory('',purpose);if(opened)$('output-dialog').showModal();
}
async function createFolder(){const name=$('new-folder').value.trim();if(!name)return;try{await api('/api/output-directories',{method:'POST',body:JSON.stringify({parent:state.directory,name})});$('new-folder').value='';await browseDirectory(state.directory)}catch(error){alert(error.message)}}

function setCatalogStatus(kind,message){const box=$('catalog-status');box.className=`catalog-status ${kind}`;box.textContent=message}

async function inspectCatalog(output){
  const schema=state.capabilities?.catalog_schema_version??8;
  state.catalog=null;$('query-catalog').disabled=true;$('report-catalog').disabled=true;$('catalog-report').hidden=true;$('catalog-body').innerHTML='';setCatalogStatus('checking',`正在检查 catalog.parquet 与 Schema v${schema} 契约…`);
  try{
    const data=await api('/api/catalog/inspect?'+new URLSearchParams({output}));state.catalog=data;
    if(data.valid){
      const versions=(data.schema_versions||[]).join(',')||'未知';setCatalogStatus('valid',`已打开 · Schema v${versions} · ${data.record_count} 条记录 · ${data.tile_count||0} 个瓦片 · ${data.catalog}`);
      $('query-catalog').disabled=false;$('report-catalog').disabled=false;
    }else setCatalogStatus('invalid',`无法打开：${(data.issues||['Catalog 校验失败']).slice(0,3).join('；')}`);
    return data.valid;
  }catch(error){setCatalogStatus('invalid',error.message);return false}
}

async function openCatalogDialog(output=$('output').value,autoQuery=false){
  $('cat-output').value=output||'';if(!$('catalog-dialog').open)$('catalog-dialog').showModal();const valid=await inspectCatalog(output);if(valid&&autoQuery)await queryCatalog();
}

async function queryCatalog(){
  if(!state.catalog?.valid){$('catalog-message').textContent='请先选择并打开通过校验的数据立方体目录。';return}
  const params=new URLSearchParams({output:$('cat-output').value,tile:$('cat-tile').value,product:$('cat-product').value,direction:$('cat-direction').value,month:$('cat-month').value});
  $('catalog-message').textContent='查询中…';
  try{
    const data=await api('/api/catalog?'+params);$('catalog-message').textContent=`共 ${data.total} 条，当前显示 ${data.returned} 条 · ${data.catalog}`;
    $('catalog-body').innerHTML=data.records.map(row=>`<tr><td>${escapeHtml(row.item_id)}</td><td>${escapeHtml(row.tile_id)}</td><td>${escapeHtml(row.product_type)}</td><td>${escapeHtml(row.flight_direction||'—')}</td><td>${escapeHtml(row.datetime||row.month||'—')}</td><td><code>${escapeHtml(row.grid_id)}</code></td><td class="path" title="${escapeHtml(row.zarr_path)}">${escapeHtml(row.zarr_path||'—')}</td><td>${escapeHtml(row.status)}</td></tr>`).join('');
  }catch(error){$('catalog-message').textContent=error.message;$('catalog-body').innerHTML=''}
}

function renderCountList(title,values){const entries=Object.entries(values||{});return `<div class="report-list"><b>${escapeHtml(title)}</b>${entries.length?entries.map(([key,value])=>`<div>${escapeHtml(key)}：${value}</div>`).join(''):'<div>无</div>'}</div>`}

async function generateCatalogReport(){
  if(!state.catalog?.valid)return;$('catalog-message').textContent='正在生成覆盖与完整性报告…';$('report-catalog').disabled=true;
  try{
    const data=await api('/api/catalog/report?'+new URLSearchParams({output:$('cat-output').value})),overall=data.overall||{},gaps=data.gaps||{},range=overall.date_range||[null,null];
    $('report-summary').innerHTML=[['记录',overall.total_records||0],['瓦片',overall.tile_count||0],['有效月份',overall.total_months||0],['存在缺月',gaps.tiles_with_gaps||0]].map(([label,value])=>`<div class="report-stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`).join('');
    $('report-counts').innerHTML=renderCountList('产品',data.counts?.products)+renderCountList('状态',data.counts?.statuses)+renderCountList('轨道',data.counts?.directions);
    $('report-gaps').innerHTML=`<div class="report-list"><div>时间范围：${escapeHtml(range[0]||'—')} 至 ${escapeHtml(range[1]||'—')}</div><div>完整组合：${gaps.tiles_complete||0}</div><div>缺月组合：${gaps.tiles_with_gaps||0}</div><div>报告瓦片行：${data.tile_rows_total||0}${data.truncated?'（页面摘要已截断）':''}</div></div>`;
    $('catalog-report').hidden=false;$('catalog-message').textContent=`报告已生成 · ${data.catalog.catalog}`;
  }catch(error){$('catalog-message').textContent=error.message}
  finally{$('report-catalog').disabled=!state.catalog?.valid}
}

async function bootstrap(){
  try{state.capabilities=await api('/api/capabilities');$('health').textContent=`已连接 · S1-GRiTS ${state.capabilities.version} · Schema v${state.capabilities.catalog_schema_version} · ${state.capabilities.stac_format==='geoparquet'?'GeoParquet':state.capabilities.stac_format}`;$('health').classList.add('ok')}catch(error){$('health').textContent='服务连接失败';$('health').classList.add('error')}
  initMap();buildTags();setWorkflow('scenes');setSelection('tiles');await loadTasks();setInterval(loadTasks,3000);
  if(location.hash==='#catalog')openCatalogDialog($('output').value);
}

document.querySelectorAll('[data-workflow]').forEach(el=>el.onclick=()=>setWorkflow(el.dataset.workflow));
document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>setSelection(el.dataset.mode));
$('apply-bbox').onclick=applyBBox;$('submit').onclick=preflight;$('confirm-run').onclick=confirmTask;$('refresh-tasks').onclick=loadTasks;
$('include-static').onchange=()=>{$('static-options').hidden=!$('include-static').checked};
$('aoi-file').onchange=async event=>{try{const file=event.target.files[0];if(!file)return;state.aoi=polygonGeometry(JSON.parse(await file.text()));$('aoi-status').textContent=`已载入 ${file.name}（EPSG:4326）`;drawAOI()}catch(error){$('aoi-status').textContent=error.message}};
$('browse-output').onclick=()=>openDirectoryBrowser('output',$('output').value);
$('close-output').onclick=()=>$('output-dialog').close();$('dir-up').onclick=()=>browseDirectory($('dir-up').dataset.parent);$('create-folder').onclick=createFolder;
$('select-output').onclick=()=>{if(state.directoryPurpose==='catalog'){$('cat-output').value=state.directory;$('output-dialog').close();inspectCatalog(state.directory)}else{$('output').value=state.directory||'s1_cube';$('output-dialog').close()}};
$('open-catalog').onclick=()=>openCatalogDialog($('output').value);$('browse-catalog').onclick=()=>openDirectoryBrowser('catalog',$('cat-output').value);
$('close-catalog').onclick=()=>$('catalog-dialog').close();$('close-catalog-bottom').onclick=()=>$('catalog-dialog').close();
$('catalog-dialog').onclick=event=>{if(event.target===$('catalog-dialog'))$('catalog-dialog').close()};
document.querySelectorAll('[data-basemap]').forEach(el=>el.onclick=()=>setBaseMap(el.dataset.basemap));$('query-catalog').onclick=queryCatalog;$('report-catalog').onclick=generateCatalogReport;
$('close-task-log').onclick=closeTaskLog;$('refresh-log').onclick=()=>pollTaskLog(false);$('task-log-dialog').onclick=event=>{if(event.target===$('task-log-dialog'))closeTaskLog()};
$('toggle-mgrs').onclick=toggleMgrs;
$('map-error').onclick=()=>{$('map-error').hidden=true};
bootstrap();
