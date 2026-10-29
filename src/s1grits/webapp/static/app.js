const state={workflow:'scenes',selectionMode:'tiles',aoi:null,aoiSource:'',candidateTiles:new Set(),selectedTiles:new Set(),plan:null,tasks:[],directory:'',directoryPurpose:'output',catalog:null,catalogRoots:[],catalogRecent:[],catalogRootId:'',catalogGeneration:0,catalogRestorePending:false,catalogFolderPath:'',catalogFolderParent:null,catalogFolderActionBusy:false,catalogCoverage:null,catalogCoverageVisible:true,catalogSelectedTile:null,workspaceTab:'tasks',capabilities:null,mgrsVisible:true,mgrsCoverage:null,recovery:null};
const queryToken=new URLSearchParams(location.search).get('token');
if(queryToken)localStorage.setItem('s1grits_web_token',queryToken);
const apiToken=queryToken||localStorage.getItem('s1grits_web_token')||'';
let map=null,aoiLayer=null,mgrsGridLayer=null,mgrsRenderer=null,catalogCoverageLayer=null,catalogRenderer=null,activeBaseLayer=null,baseMode='road';
let mgrsRequest=null,mgrsRequestSerial=0,mgrsLoadTimer=null,mgrsPermanentLabels=false,mgrsLabelsEnabled=true,mapNoticeTimer=null;
let activeLog=null,logPollTimer=null,tileInputTimer=null,sessionSaveTimer=null,catalogFolderRequestSerial=0;
const currentMgrsLayers=new Map(),currentCatalogLayers=new Map(),baseProviderIndex={road:0,satellite:0};
const MGRS_VIEW_PADDING=.2;
const $=id=>document.getElementById(id);
const escapeHtml=value=>String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

async function api(path,options={}){
  const auth=apiToken?{'Authorization':`Bearer ${apiToken}`}:{},isForm=options.body instanceof FormData;
  const headers={...auth,...(isForm?{}:{'Content-Type':'application/json'}),...(options.headers||{})};
  const response=await fetch(path,{...options,headers});
  const raw=await response.text();let body={};try{body=raw?JSON.parse(raw):{}}catch{body={error:raw}}
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

function parsedTiles(value){return [...new Set(String(value||'').split(/[\s,;]+/).map(item=>item.trim().toUpperCase()).filter(Boolean))].sort()}
function scheduleTileSessionSave(){clearTimeout(sessionSaveTimer);sessionSaveTimer=setTimeout(()=>sessionStorage.setItem('s1grits_selected_tiles',JSON.stringify([...state.selectedTiles].sort())),250)}
function updateVisibleTileStyle(tileId){const layer=currentMgrsLayers.get(tileId);if(layer)layer.setStyle(mgrsStyle(layer.feature));const catalogLayer=currentCatalogLayers.get(tileId);if(catalogLayer)catalogLayer.setStyle(catalogStyle(catalogLayer.feature))}
function refreshVisibleMgrsStyles(){currentMgrsLayers.forEach(layer=>layer.setStyle(mgrsStyle(layer.feature)));currentCatalogLayers.forEach(layer=>layer.setStyle(catalogStyle(layer.feature)))}
function syncTileSelection({fromText=false,changedTile=null,bulk=false}={}){
  if(fromText){const parsed=parsedTiles($('tiles').value);state.selectedTiles=new Set(state.aoi?parsed.filter(id=>state.candidateTiles.has(id)):parsed)}
  const tiles=[...state.selectedTiles].sort();$('tiles').value=tiles.join('\n');$('selected-count').textContent=tiles.length;scheduleTileSessionSave();
  if(bulk||fromText)refreshVisibleMgrsStyles();else if(changedTile)updateVisibleTileStyle(changedTile);
}

function setTileSelected(tileId,selected){
  const id=String(tileId).toUpperCase();if(selected)state.selectedTiles.add(id);else state.selectedTiles.delete(id);syncTileSelection({changedTile:id});
}

function toggleTile(tileId){
  const id=String(tileId).toUpperCase();
  if(state.aoi&&!state.candidateTiles.has(id)){setMgrsStatus(`${id} 不在当前 AOI 候选范围；先清除 AOI 才能选择`,true);return}
  setTileSelected(id,!state.selectedTiles.has(id));
  setMgrsStatus(`${state.selectedTiles.has(id)?'已选择':'已取消'} ${id}`);if(map)map.closePopup();
}

function clearTiles(){state.selectedTiles.clear();syncTileSelection({bulk:true})}
function selectCandidates(selected=true){state.candidateTiles.forEach(id=>selected?state.selectedTiles.add(id):state.selectedTiles.delete(id));syncTileSelection({bulk:true})}
function clearAOI(){state.aoi=null;state.aoiSource='';state.candidateTiles.clear();$('aoi-file').value='';$('aoi-status').textContent='当前未设置 AOI。';$('select-candidates').disabled=true;$('deselect-candidates').disabled=true;$('clear-aoi').disabled=true;drawAOI();refreshVisibleMgrsStyles()}

function showMapError(message,autoHideMs=0){
  const box=$('map-error');clearTimeout(mapNoticeTimer);box.textContent=message;box.hidden=false;
  if(autoHideMs>0)mapNoticeTimer=setTimeout(()=>{box.hidden=true},autoHideMs);
}

function setMgrsWarning(message=''){
  const button=$('toggle-mgrs');button.classList.toggle('warning',Boolean(message));
  button.title=message||'缩放至4级后显示MGRS格网';
}

function setMgrsStatus(message,isError=false){if(isError){setMgrsWarning(message);showMapError(message,4500)}else showMapStatus(message)}

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
  setTileSelected(tileId,true);setMgrsStatus(`已选择 ${tileId}`);if(map)map.closePopup();
}

function showMapStatus(message='',kind='info'){
  const box=$('map-status');box.textContent=message;box.className=`map-status ${kind}`;box.hidden=!message;
}

function bindMgrsFeature(feature,layer){
  const tileId=feature.properties.tile_id,epsg=feature.properties.utm_epsg;
  currentMgrsLayers.set(tileId,layer);
  if(mgrsLabelsEnabled)layer.bindTooltip(tileId,{sticky:!mgrsPermanentLabels,permanent:mgrsPermanentLabels,direction:'center',className:'mgrs-label'});
  layer.on('click',()=>{toggleTile(tileId);setMgrsStatus(`${tileId} · EPSG:${epsg} · ${state.selectedTiles.has(tileId)?'已选择':'未选择'}`)});
}

function mgrsStyle(feature){
  const id=feature?.properties?.tile_id;
  const satellite=baseMode==='satellite';
  if(state.selectedTiles.has(id))return {color:satellite?'#00e5ff':'#007c91',weight:2.2,opacity:1,fillColor:satellite?'#00c8e6':'#00a7bd',fillOpacity:satellite ? .16 : .20,interactive:true};
  if(state.candidateTiles.has(id))return {color:satellite?'#ffb000':'#e07800',weight:1.6,opacity:.98,fillColor:'#ff9800',fillOpacity:satellite ? .10 : .13,dashArray:'7 4',interactive:true};
  return {color:satellite?'#ffd54f':'#425f75',weight:satellite ? 1.1 : .8,opacity:satellite ? .96 : .72,fill:false,interactive:true};
}

async function loadMgrsGrid(force=false){
  if(!map||!mgrsGridLayer)return;
  if(!state.mgrsVisible){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning();showMapStatus('MGRS参考格网已隐藏');return}
  const minZoom=state.capabilities?.mgrs_map?.min_zoom??4,zoom=map.getZoom();
  if(zoom<minZoom){currentMgrsLayers.clear();mgrsGridLayer.clearLayers()}
  if(!force&&viewportCovered(state.mgrsCoverage))return;
  const coverage=expandedViewport(),bbox=[coverage.west,coverage.south,coverage.east,coverage.north].map(value=>value.toFixed(6)).join(',');
  if(mgrsRequest)mgrsRequest.abort();mgrsRequest=new AbortController();const serial=++mgrsRequestSerial;
  setMgrsStatus('正在加载MGRS格网…');
  try{
    const data=await api(`/api/map/mgrs?bbox=${encodeURIComponent(bbox)}&zoom=${zoom}`,{signal:mgrsRequest.signal});
    if(serial!==mgrsRequestSerial)return;
    if(!data.visible){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage={...coverage,zoom};const message=data.message||`当前视窗包含 ${data.count} 个 MGRS 格网；缩放至 ${minZoom} 级后显示边界`;setMgrsWarning(message);showMapStatus(message,'warning');return}
    if(data.truncated){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage={...coverage,zoom};const message=`当前视窗包含 ${data.count} 个 MGRS 格网，超过单次绘制上限 ${data.max_features}，请继续放大`;setMgrsWarning(message);showMapStatus(message,'warning');return}
    setMgrsWarning();mgrsLabelsEnabled=data.returned<=800;mgrsPermanentLabels=zoom>=7&&data.returned<=180;currentMgrsLayers.clear();mgrsGridLayer.clearLayers();mgrsGridLayer.addData(data.features);
    state.mgrsCoverage={...coverage,zoom};setMgrsStatus(`已显示 ${data.returned} 个 MGRS 格网 · z${zoom}`);
  }catch(error){
    if(error.name==='AbortError')return;
    state.mgrsCoverage=null;setMgrsStatus(`MGRS参考格网加载失败：${error.message}`,true);
  }
}

function scheduleMgrsLoad(force=false){
  clearTimeout(mgrsLoadTimer);mgrsLoadTimer=setTimeout(()=>loadMgrsGrid(force),180);
}

const DEFAULT_BASEMAPS={
  road:[
    {name:'Google 道路',url:'https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',attribution:'&copy; Google',max_zoom:19,subdomains:['mt0','mt1','mt2','mt3']},
    {name:'OpenStreetMap（备用）',url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'&copy; OpenStreetMap contributors',max_zoom:19}
  ],
  satellite:[
    {name:'Google 卫星',url:'https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',attribution:'Imagery &copy; Google',max_zoom:19,subdomains:['mt0','mt1','mt2','mt3']},
    {name:'Esri 卫星（备用）',url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',attribution:'Tiles &copy; Esri',max_zoom:19}
  ]
};

function basemapProviders(mode){const configured=state.capabilities?.basemaps?.[mode];return Array.isArray(configured)&&configured.length?configured:DEFAULT_BASEMAPS[mode]}

function activateBaseMap(mode,{resetProvider=false}={}){
  baseMode=mode;if(!map)return;if(resetProvider)baseProviderIndex[mode]=0;
  if(activeBaseLayer&&map.hasLayer(activeBaseLayer))map.removeLayer(activeBaseLayer);
  const providers=basemapProviders(mode),index=Math.min(baseProviderIndex[mode],providers.length-1),provider=providers[index];baseProviderIndex[mode]=index;
  let errors=0,loaded=false;
  const options={maxZoom:provider.max_zoom||19,attribution:provider.attribution||'',keepBuffer:2,noWrap:true};
  if(Array.isArray(provider.subdomains)&&provider.subdomains.length)options.subdomains=provider.subdomains;
  const layer=L.tileLayer(provider.url,options);
  layer.on('tileload',()=>{loaded=true;errors=0});
  layer.on('tileerror',()=>{errors+=1;if(errors<4||layer!==activeBaseLayer)return;const next=index+1;if(next<providers.length){baseProviderIndex[mode]=next;showMapError(`${provider.name} 无法访问，正在切换到 ${providers[next].name}…`,3000);activateBaseMap(mode)}else if(!loaded){showMapError(`${provider.name} 暂时无法访问；MGRS 与 AOI 仍可正常使用。`)}});
  activeBaseLayer=layer;layer.addTo(map);
  document.querySelectorAll('[data-basemap]').forEach(el=>{el.classList.toggle('active',el.dataset.basemap===mode);if(el.dataset.basemap===mode)el.title=`当前：${provider.name}`});
}

function catalogStyle(feature){
  const tileId=feature?.properties?.tile_id,count=Number(feature?.properties?.record_count)||0,fillOpacity=count>200 ? .38 : count>50 ? .28 : count>10 ? .20 : .12;
  if(state.selectedTiles.has(tileId))return {color:baseMode==='satellite'?'#00e5ff':'#007c91',weight:3,opacity:1,fillColor:'#a855f7',fillOpacity,interactive:true};
  return {color:baseMode==='satellite'?'#f45cff':'#8b2fc9',weight:2,opacity:.98,fillColor:baseMode==='satellite'?'#d946ef':'#8b5cf6',fillOpacity,interactive:true};
}

function bindCatalogFeature(feature,layer){
  const p=feature.properties||{},tileId=String(p.tile_id||''),statuses=Object.entries(p.status_counts||{}).map(([key,value])=>`${key} ${value}`).join(' / ')||'—';
  currentCatalogLayers.set(tileId,layer);
  layer.bindPopup(`<div class="catalog-popup"><b>${escapeHtml(tileId)}</b><dl><dt>记录</dt><dd>${escapeHtml(p.record_count||0)} 条</dd><dt>产品</dt><dd>${escapeHtml((p.product_types||[]).join(' / ')||'—')}</dd><dt>轨道</dt><dd>${escapeHtml((p.directions||[]).join(' / ')||'—')}</dd><dt>时间</dt><dd>${escapeHtml(p.month_min||'—')} 至 ${escapeHtml(p.month_max||'—')}</dd><dt>状态</dt><dd>${escapeHtml(statuses)}</dd><dt>格网</dt><dd>${escapeHtml(p.grid_count||0)} 个版本</dd></dl><div class="popup-actions"><button type="button" onclick="addCatalogTile('${escapeHtml(tileId)}')">加入下载选择</button><button type="button" onclick="openCatalogTileDetails('${escapeHtml(tileId)}')">查看详细记录</button></div></div>`);
  layer.on('click',()=>{state.catalogSelectedTile=tileId;showMapStatus(`本地数据 ${tileId} · ${p.record_count||0} 条记录`)});
}

function addCatalogTile(tileId){setSelection('tiles');setTileSelected(tileId,true);showMapStatus(`已将 ${tileId} 加入下载选择`)}

function toggleCatalogCoverage(){
  if(!catalogCoverageLayer||!state.catalogCoverage)return;
  state.catalogCoverageVisible=!state.catalogCoverageVisible;const button=$('toggle-local-data');button.classList.toggle('active',state.catalogCoverageVisible);button.setAttribute('aria-pressed',String(state.catalogCoverageVisible));
  if(state.catalogCoverageVisible){if(!map.hasLayer(catalogCoverageLayer))catalogCoverageLayer.addTo(map);showMapStatus(`已显示 ${state.catalogCoverage.mapped_tile_count||0} 个本地数据格网`)}else{if(map.hasLayer(catalogCoverageLayer))map.removeLayer(catalogCoverageLayer);showMapStatus('本地数据覆盖已隐藏')}
}

function initMap(){
  if(typeof L==='undefined'){showMapError('项目内置 Leaflet 资源未加载，请重新安装 S1-GRiTS Web 组件并重启服务。');return}
  map=L.map('map',{center:[34.5,105],zoom:4,zoomControl:true,worldCopyJump:false,preferCanvas:true,maxBounds:[[-85.051129,-180],[85.051129,180]],maxBoundsViscosity:1});
  map.createPane('mgrsReferencePane');map.getPane('mgrsReferencePane').style.zIndex=410;
  map.createPane('catalogCoveragePane');map.getPane('catalogCoveragePane').style.zIndex=430;
  map.createPane('aoiPane');map.getPane('aoiPane').style.zIndex=440;
  mgrsRenderer=L.canvas({pane:'mgrsReferencePane',padding:.5});
  catalogRenderer=L.canvas({pane:'catalogCoveragePane',padding:.5});
  mgrsGridLayer=L.geoJSON(null,{renderer:mgrsRenderer,pane:'mgrsReferencePane',style:mgrsStyle,onEachFeature:bindMgrsFeature}).addTo(map);
  catalogCoverageLayer=L.geoJSON(null,{renderer:catalogRenderer,pane:'catalogCoveragePane',style:catalogStyle,onEachFeature:bindCatalogFeature}).addTo(map);
  aoiLayer=L.layerGroup().addTo(map);activateBaseMap('road',{resetProvider:true});
  map.on('moveend zoomend',()=>scheduleMgrsLoad());scheduleMgrsLoad(true);
  window.addEventListener('resize',()=>map.invalidateSize());setTimeout(()=>map.invalidateSize(),100);
}

function setBaseMap(mode){
  activateBaseMap(mode,{resetProvider:true});refreshVisibleMgrsStyles();if(catalogCoverageLayer)catalogCoverageLayer.setStyle(catalogStyle);
}

function toggleMgrs(){
  state.mgrsVisible=!state.mgrsVisible;const button=$('toggle-mgrs');button.classList.toggle('active',state.mgrsVisible);button.setAttribute('aria-pressed',String(state.mgrsVisible));
  if(state.mgrsVisible){if(!map.hasLayer(mgrsGridLayer))mgrsGridLayer.addTo(map);scheduleMgrsLoad(true)}else{if(mgrsRequest)mgrsRequest.abort();currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning();showMapStatus('MGRS参考格网已隐藏')}
}

function drawAOI(){
  if(!map||!aoiLayer)return;aoiLayer.clearLayers();
  if(state.aoi){const layer=L.geoJSON(state.aoi,{pane:'aoiPane',interactive:false,style:{color:'#c8781b',weight:2,dashArray:'7 5',fillColor:'#e49b33',fillOpacity:.10,interactive:false}}).addTo(aoiLayer);map.fitBounds(layer.getBounds(),{padding:[25,25],maxZoom:9})}
}

function fitTileFeatures(features){
  if(!map||!features?.length)return;let bounds=null;
  const visit=value=>{if(Array.isArray(value)&&value.length>=2&&Number.isFinite(Number(value[0]))&&Number.isFinite(Number(value[1]))){const point=L.latLng(Number(value[1]),Number(value[0]));bounds=bounds?bounds.extend(point):L.latLngBounds(point,point)}else if(Array.isArray(value))value.forEach(visit)};
  features.forEach(feature=>visit(feature.geometry?.coordinates));if(bounds?.isValid())map.fitBounds(bounds,{padding:[28,28],maxZoom:9});
}

function drawPlan(plan){
  fitTileFeatures(plan.tile_features||[]);
}

function applyAOIResult(result,{selectAll=true}={}){
  state.aoi=result.geometry;state.aoiSource=result.source||'AOI';state.candidateTiles=new Set(result.candidate_tiles||[]);
  if(selectAll){state.selectedTiles=new Set(state.candidateTiles);syncTileSelection({bulk:true})}else syncTileSelection({bulk:true});
  $('select-candidates').disabled=!state.candidateTiles.size;$('deselect-candidates').disabled=!state.candidateTiles.size;$('clear-aoi').disabled=false;
  $('aoi-status').textContent=`${state.aoiSource} · ${result.source_crs} → EPSG:4326 · ${result.candidate_count} 个候选格网${result.candidate_count>result.max_task_tiles?`（最终最多选择 ${result.max_task_tiles} 个）`:''}`;
  drawAOI();
}

async function applyBBox(){
  const w=Number($('west').value),e=Number($('east').value),s=Number($('south').value),n=Number($('north').value);
  if(![w,e,s,n].every(Number.isFinite)||w>=e||s>=n||w<70||e>140||s<10||n>55){
    $('aoi-status').textContent='请输入中国区域内有效的西、东、南、北边界。'; return;
  }
  const geometry={type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]};
  $('aoi-status').textContent='正在计算 AOI 与 MGRS 候选关系…';
  try{applyAOIResult(await api('/api/spatial/aoi/resolve',{method:'POST',body:JSON.stringify({geometry})}))}catch(error){$('aoi-status').textContent=error.message}
}

async function uploadAOI(files){
  if(!files.length)return;const form=new FormData();files.forEach(file=>form.append('files',file,file.name));$('aoi-status').textContent='正在上传、转换投影并计算候选格网…';
  try{applyAOIResult(await api('/api/spatial/aoi/resolve',{method:'POST',body:form}))}catch(error){$('aoi-status').textContent=error.message}
}

function selected(selector){return [...document.querySelectorAll(selector+':checked')].map(el=>Number(el.value))}
function payload(){
  return {
    workflow:state.workflow,selection_mode:'tiles',
    tiles:[...state.selectedTiles].sort(),direction:$('direction').value,
    years:selected('#years input'),months:selected('#months input'),output_subdir:$('output').value,
    zarr_only:$('zarr-only').checked,include_static:$('include-static').checked,smonthly:$('smonthly').checked,
    target_resolution:Number($('resolution').value),resampling_method:'auto',max_workers:Number($('workers').value),
    spatial_despeckle:$('despeckle').checked,features_ratio:$('ratio').checked,features_rvi:$('rvi').checked,
    static_layers:[...document.querySelectorAll('[name=static-layer]:checked')].map(el=>el.value)
  };
}

async function preflight(){
  const button=$('submit'); button.disabled=true; $('form-message').textContent='正在执行空间、容量与配置预检…';
  try{
    const plan=await api('/api/plan',{method:'POST',body:JSON.stringify(payload())}); state.plan=plan; drawPlan(plan);
    const temporal=`${plan.years.join('、')} 年 · ${plan.months.length} 个月份`,products=plan.include_static?`${plan.workflow} + static`:plan.workflow;
    $('plan-summary').innerHTML=`<dl><dt>产品</dt><dd>${escapeHtml(products)}</dd><dt>目标格网</dt><dd>${plan.target_resolution} 米 · ${plan.resampling_method==='bilinear'?'双线性（线性功率域）':'最近邻'}</dd><dt>瓦片</dt><dd>${plan.tiles.length} 个：${escapeHtml(plan.tiles.slice(0,18).join(', '))}${plan.tiles.length>18?' …':''}</dd><dt>轨道</dt><dd>${escapeHtml(plan.directions.join(' → '))}</dd><dt>时间</dt><dd>${escapeHtml(temporal)}</dd><dt>输出</dt><dd><code>${escapeHtml(plan.output_dir)}</code></dd><dt>规划估算</dt><dd>${plan.raw_gib.toFixed(3)} GiB</dd></dl><p class="hint">${escapeHtml(plan.estimate_note)}</p>`;
    const needs=Boolean(plan.confirmation_required);$('phrase-wrap').hidden=!needs;$('phrase').value='';$('phrase-example').textContent=plan.confirmation_phrase||'';$('phrase-reason').textContent=plan.confirmation_reason||'';$('confirm-run').disabled=needs;
    $('confirm-dialog').showModal(); $('form-message').textContent='预检通过，请核对规划。';
  }catch(error){$('form-message').textContent=error.message}
  finally{button.disabled=false}
}

function updateConfirmation(){const required=Boolean(state.plan?.confirmation_required);$('confirm-run').disabled=required&&$('phrase').value!==state.plan.confirmation_phrase}

async function copyConfirmation(){if(!state.plan?.confirmation_phrase)return;try{await navigator.clipboard.writeText(state.plan.confirmation_phrase)}catch{$('phrase').value=state.plan.confirmation_phrase}updateConfirmation()}

async function confirmTask(event){
  event.preventDefault(); if(!state.plan)return;
  const button=$('confirm-run'); button.disabled=true;
  try{
    const task=await api('/api/tasks',{method:'POST',body:JSON.stringify({plan_id:state.plan.plan_id,confirmation:$('phrase').value})});
    $('confirm-dialog').close(); state.plan=null; $('form-message').textContent=`任务 ${task.run_id} 已进入队列。`; await loadTasks();
  }catch(error){$('form-message').textContent=error.message}
  finally{button.disabled=false}
}

const statusNames={queued:'排队中',running:'处理中',detached:'外部处理中',processing:'影像处理',static:'静态图层',validating:'目录复验',associating:'关联复验',done:'已完成',failed:'失败',cancelled:'已取消',cancelling:'取消中',interrupted:'已中断'};
function taskCard(task){
  const progress=Math.max(0,Math.min(1,Number(task.progress)||0));
  const temporal=`${(task.years||[]).join(',')} · ${(task.months||[]).length}月`;
  const productLabel=task.include_static?`${task.workflow} + static`:task.workflow;
  const cancel=!['done','failed','cancelled','interrupted','detached'].includes(task.status)?`<button class="secondary" onclick="cancelTask('${task.run_id}')">取消</button>`:'';
  const recovery=task.recoverable?`<button class="primary" onclick="openRecovery('${task.run_id}')">恢复任务</button>`:'';
  const association=task.validation?.static_association?` · Static关联 ${task.validation.static_association.pairs_checked}`:'';
  const validation=task.validation?`<div class="task-meta ok">Catalog ${task.validation.records} 条 · ${task.validation.tiles} 瓦片${association}</div>`:'';
  const error=task.error?`<div class="task-meta error" title="${escapeHtml(task.error)}">${escapeHtml(task.error)}</div>`:'';
  const catalogArgument=escapeHtml(JSON.stringify(task.output_subdir||''));
  return `<article class="task-card"><div class="task-title"><b>${escapeHtml(productLabel)} · ${(task.tiles||[]).length} 瓦片</b><span class="badge ${escapeHtml(task.status)}">${statusNames[task.status]||statusNames[task.stage]||escapeHtml(task.status)}</span></div><div class="task-meta">${escapeHtml((task.directions||[]).join(' + '))} · ${escapeHtml(temporal)}</div><div class="task-meta" title="${escapeHtml(task.output_dir)}">${escapeHtml(task.output_subdir)}</div><div class="progress"><i style="width:${progress*100}%"></i></div>${validation}${error}<div class="task-actions"><button class="secondary" onclick="openLog('${task.run_id}')">日志</button>${recovery}${cancel}<button class="secondary" onclick="catalogFor(${catalogArgument})">检索结果</button></div></article>`;
}

async function loadTasks(){
  try{state.tasks=await api('/api/tasks'); $('tasks').innerHTML=state.tasks.length?state.tasks.map(taskCard).join(''):''}catch(error){$('tasks').innerHTML=`<div class="empty error">${escapeHtml(error.message)}</div>`}
}
async function cancelTask(id){if(!confirm('确认取消该任务及其子进程？'))return; try{await api(`/api/tasks/${id}`,{method:'DELETE'});await loadTasks()}catch(error){alert(error.message)}}

async function openRecovery(id){
  $('recovery-summary').innerHTML='<p>正在检查原配置、输出目录和并发状态…</p>';$('confirm-recovery').disabled=true;$('recovery-dialog').showModal();
  try{
    const check=await api(`/api/tasks/${encodeURIComponent(id)}/recovery`);state.recovery=check;
    const issues=(check.issues||[]).map(value=>`<li>${escapeHtml(value)}</li>`).join(''),warnings=(check.warnings||[]).map(value=>`<li>${escapeHtml(value)}</li>`).join('');
    $('recovery-summary').innerHTML=`<p class="${check.recoverable?'ok':'error'}">${escapeHtml(check.message)}</p><dl><dt>输出目录</dt><dd><code>${escapeHtml(check.output_dir)}</code></dd><dt>恢复位置</dt><dd>第 ${check.resume_command}/${check.command_total} 条受控命令</dd><dt>执行次数</dt><dd>第 ${check.attempt_next} 次</dd><dt>现有成果</dt><dd>${check.zarr_count} 个 Zarr · Catalog ${check.catalog_exists?'已存在':'尚未生成'}</dd></dl>${issues?`<h3>阻止恢复</h3><ul class="error">${issues}</ul>`:''}${warnings?`<h3>恢复说明</h3><ul>${warnings}</ul>`:''}<p class="hint">恢复不会删除已有输出；完整月份将跳过，中断附近内容可能重新下载或处理。完成后会重新执行 Catalog 门禁。</p>`;
    $('confirm-recovery').disabled=!check.recoverable;
  }catch(error){state.recovery=null;$('recovery-summary').innerHTML=`<p class="error">${escapeHtml(error.message)}</p>`}
}

async function confirmRecovery(){if(!state.recovery?.recoverable)return;const button=$('confirm-recovery');button.disabled=true;try{await api(`/api/tasks/${encodeURIComponent(state.recovery.job_id)}/resume`,{method:'POST'});$('recovery-dialog').close();state.recovery=null;await loadTasks()}catch(error){$('recovery-summary').insertAdjacentHTML('beforeend',`<p class="error">${escapeHtml(error.message)}</p>`)}finally{button.disabled=false}}
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
function setWorkspaceTab(tab){
  state.workspaceTab=tab;const tasks=tab==='tasks';$('tasks-tab').classList.toggle('active',tasks);$('local-search-tab').classList.toggle('active',!tasks);$('tasks-tab').setAttribute('aria-selected',String(tasks));$('local-search-tab').setAttribute('aria-selected',String(!tasks));$('tasks-pane').hidden=!tasks;$('local-search-pane').hidden=tasks;$('refresh-tasks').hidden=!tasks;
}
const CATALOG_SELECTION_KEY='s1grits_catalog_selection',CATALOG_RECENT_KEY='s1grits_catalog_recent';

function catalogRoot(rootId){return state.catalogRoots.find(item=>item.root_id===rootId)||null}
function catalogPath(root,output=''){if(!root)return'';if(!output)return root.path_display;const separator=root.path_display.includes('\\')?'\\':'/';return root.path_display.replace(/[\\/]$/,'')+separator+String(output).replaceAll('/',separator)}
function catalogName(root,output=''){const part=String(output||'').split('/').filter(Boolean).at(-1);return part||root?.label||'本地数据立方体'}
function readJsonStorage(key,fallback){try{return JSON.parse(localStorage.getItem(key)||'null')??fallback}catch{return fallback}}

function renderCatalogRecent(){
  const stored=readJsonStorage(CATALOG_RECENT_KEY,[]),known=new Set(),recent=[];
  for(const item of Array.isArray(stored)?stored:[]){const root=catalogRoot(item.root_id);if(!root||known.has(`${item.root_id}|${item.output||''}`))continue;recent.push({root_id:item.root_id,output:item.output||'',touched_at:item.touched_at||0});known.add(`${item.root_id}|${item.output||''}`)}
  state.catalogRecent=recent.slice(0,8);const wrap=$('recent-catalogs'),list=$('recent-catalog-list');wrap.hidden=!state.catalogRecent.length;
  list.innerHTML=state.catalogRecent.map(item=>{const root=catalogRoot(item.root_id),path=catalogPath(root,item.output);return `<button type="button" data-root-id="${escapeHtml(item.root_id)}" data-output="${escapeHtml(item.output)}" title="${escapeHtml(path)}">📁 ${escapeHtml(catalogName(root,item.output))} · ${escapeHtml(path)}</button>`}).join('');
  document.querySelectorAll('#recent-catalog-list button').forEach(button=>button.onclick=()=>openCatalogSelection(catalogRoot(button.dataset.rootId),button.dataset.output||''));
}

function rememberCatalogSelection(rootId,output){
  const entry={root_id:rootId,output:output||'',touched_at:Date.now()},stored=readJsonStorage(CATALOG_RECENT_KEY,[]),recent=[entry,...(Array.isArray(stored)?stored:[]).filter(item=>item.root_id!==rootId||String(item.output||'')!==entry.output)].slice(0,8);
  localStorage.setItem(CATALOG_SELECTION_KEY,JSON.stringify(entry));localStorage.setItem(CATALOG_RECENT_KEY,JSON.stringify(recent));renderCatalogRecent();
}

function showCatalogEmpty(message=''){
  state.catalogGeneration++;state.catalogRestorePending=false;state.catalog=null;state.catalogRootId='';$('cat-output').value='';$('catalog-empty').hidden=false;$('catalog-selection').hidden=true;$('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=true;$('catalog-report').hidden=true;$('catalog-home-message').textContent=message;$('query-catalog').disabled=true;$('report-catalog').disabled=true;if($('catalog-detail-dialog').open)$('catalog-detail-dialog').close();clearCatalogMap();
}

function resetCatalogFilters(){$('cat-tile').value='';$('cat-product').value='';$('cat-direction').value='';$('cat-month').value='';$('cat-status').value=''}

function prepareCatalogSelection(root,output=''){
  if(!root){showCatalogEmpty('所选文件夹记录已经失效，请重新选择。');return false}state.catalogGeneration++;state.catalogRestorePending=false;state.catalogRootId=root.root_id;$('cat-output').value=output||'';state.catalog=null;resetCatalogFilters();$('catalog-empty').hidden=true;$('catalog-selection').hidden=false;$('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=true;$('catalog-report').hidden=true;$('catalog-candidate-list').innerHTML='';$('catalog-selection-name').textContent=catalogName(root,output);$('catalog-selection-path').textContent=catalogPath(root,output);$('catalog-selection-path').title=catalogPath(root,output);$('forget-catalog-folder').hidden=!root.removable;$('query-catalog').disabled=true;$('report-catalog').disabled=true;$('catalog-message').textContent='';if($('catalog-detail-dialog').open)$('catalog-detail-dialog').close();clearCatalogMap();setCatalogStatus('checking','正在检查 catalog.parquet 与数据结构…');return true;
}

function showCatalogDiscovery(){
  $('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=false;$('catalog-candidate-list').innerHTML='';$('catalog-discovery-message').textContent='所选目录根部不存在 catalog.parquet。可以重新选择，或仅检查当前目录的一级子目录。';setCatalogStatus('invalid','当前文件夹不是可直接打开的数据立方体');
}

async function loadCatalogRoots(preferredRootId=null){
  try{const data=await api('/api/catalog-roots');state.catalogRoots=data.roots||[];renderCatalogRecent();return catalogRoot(preferredRootId)}catch(error){state.catalogRoots=[];renderCatalogRecent();showCatalogEmpty(`本地数据目录加载失败：${error.message}`);return null}
}

async function openCatalogSelection(root,output='',{autoQuery=true,activateTab=true}={}){
  if(!prepareCatalogSelection(root,output))return false;const generation=state.catalogGeneration;if(activateTab)setWorkspaceTab('catalog');const valid=await inspectCatalog(output,generation);
  if(valid){if(autoQuery)await queryCatalog(generation);return true}
  if((state.catalog?.issues||[]).some(issue=>String(issue).includes('不存在 catalog.parquet')))showCatalogDiscovery();return false;
}

async function restoreCatalogSelection(){
  const saved=readJsonStorage(CATALOG_SELECTION_KEY,null),root=saved?catalogRoot(saved.root_id):null;if(!root){showCatalogEmpty();return}if(location.hash==='#catalog'){await openCatalogSelection(root,saved.output||'',{autoQuery:true,activateTab:true});return}prepareCatalogSelection(root,saved.output||'');state.catalogRestorePending=true;setCatalogStatus('neutral','上次使用的数据已恢复；进入“本地数据”时将重新校验。');
}

async function catalogFor(output){
  if(!state.catalogRoots.length)await loadCatalogRoots();const root=catalogRoot('workspace');setWorkspaceTab('catalog');await openCatalogSelection(root,output||'',{autoQuery:true});
}

function openManualCatalogDialog(){
  $('catalog-root-path').value='';$('catalog-root-label').value='';$('catalog-root-message').textContent='';$('catalog-root-dialog').showModal();
}

function setCatalogFolderBrowserBusy(busy,{action=false}={}){
  state.catalogFolderActionBusy=Boolean(busy&&action);$('catalog-folder-list').setAttribute('aria-busy',String(busy));$('catalog-folder-up').disabled=busy||state.catalogFolderParent===null;$('select-current-catalog-folder').disabled=busy||!state.catalogFolderPath;$('retry-catalog-folders').disabled=busy;$('manual-catalog-folder').disabled=busy;$('close-catalog-folder').disabled=state.catalogFolderActionBusy;$('cancel-catalog-folder').disabled=state.catalogFolderActionBusy;document.querySelectorAll('#catalog-folder-list button').forEach(button=>{button.disabled=busy});
}

function closeCatalogFolderDialog(force=false){
  if(state.catalogFolderActionBusy&&!force)return;catalogFolderRequestSerial++;state.catalogFolderActionBusy=false;const dialog=$('catalog-folder-dialog');if(dialog.open)dialog.close();
}

function folderBrowserMessage(message){$('catalog-folder-list').innerHTML=`<div class="folder-browser-message">${escapeHtml(message)}</div>`}

async function loadCatalogFolders(path=''){
  const dialog=$('catalog-folder-dialog'),requested=String(path??''),serial=++catalogFolderRequestSerial;if(!dialog.open)return false;setCatalogFolderBrowserBusy(true);$('catalog-folder-status').className='catalog-folder-status';$('catalog-folder-status').textContent=requested?`正在打开 ${requested}…`:'正在读取本机盘符…';$('retry-catalog-folders').hidden=true;folderBrowserMessage('正在加载文件夹…');
  try{
    const data=await api('/api/catalog-folders?'+new URLSearchParams({path:requested}));if(serial!==catalogFolderRequestSerial||!dialog.open)return false;const directoryItems=Array.isArray(data.directories)?data.directories:[],driveItems=Array.isArray(data.drives)?data.drives:[],directories=(data.mode==='drives'||(!requested&&driveItems.length)?driveItems:directoryItems).map(item=>({...item,has_catalog:Boolean(item.has_catalog??item.catalog_available)}));state.catalogFolderPath=String(data.current_path??data.path??requested??'');const parentValue=data.parent_path??data.parent;state.catalogFolderParent=parentValue===null||parentValue===undefined?null:String(parentValue);const current=state.catalogFolderPath||'此电脑';$('catalog-folder-current').textContent=current;$('catalog-folder-current').title=current;$('catalog-folder-up').dataset.path=state.catalogFolderParent??'';
    $('catalog-folder-list').innerHTML=directories.length?directories.map(item=>`<div class="catalog-folder-row" role="listitem"><button class="catalog-folder-entry" type="button" data-path="${escapeHtml(item.path)}" aria-label="进入文件夹 ${escapeHtml(item.name)}"><span class="folder-icon" aria-hidden="true">📁</span><span class="folder-name">${escapeHtml(item.name)}</span>${item.has_catalog?'<span class="catalog-ready">含 Catalog</span>':''}<span class="folder-enter" aria-hidden="true">进入 ›</span></button></div>`).join(''):'<div class="folder-browser-message">当前目录没有可进入的子文件夹。</div>';document.querySelectorAll('#catalog-folder-list .catalog-folder-entry').forEach(button=>button.onclick=()=>loadCatalogFolders(button.dataset.path));const catalogCount=directories.filter(item=>item.has_catalog).length;$('catalog-folder-status').textContent=state.catalogFolderPath?`发现 ${directories.length} 个子文件夹${catalogCount?`，其中 ${catalogCount} 个包含 Catalog`:''}。`:'请选择一个盘符或可用位置。';setCatalogFolderBrowserBusy(false);return true;
  }catch(error){
    if(serial!==catalogFolderRequestSerial||!dialog.open)return false;$('catalog-folder-status').className='catalog-folder-status error';$('catalog-folder-status').textContent=`文件夹加载失败：${error.message}`;$('retry-catalog-folders').dataset.path=requested;$('retry-catalog-folders').hidden=false;folderBrowserMessage('无法读取该位置。请重试，或使用下方其他方式。');setCatalogFolderBrowserBusy(false);$('select-current-catalog-folder').disabled=true;return false;
  }
}

function openCatalogFolderDialog(){
  const dialog=$('catalog-folder-dialog');state.catalogFolderPath='';state.catalogFolderParent=null;state.catalogFolderActionBusy=false;$('catalog-folder-current').textContent='此电脑';$('catalog-folder-current').title='此电脑';if(!dialog.open)dialog.showModal();loadCatalogFolders('');
}

function openManualCatalogFromBrowser(){closeCatalogFolderDialog(true);openManualCatalogDialog()}

async function acceptCatalogRootResponse(data){
  if(data.cancelled){const message='已取消选择，当前本地数据未改变。';if($('catalog-empty').hidden)$('catalog-message').textContent=message;else $('catalog-home-message').textContent=message;return false}const root=await loadCatalogRoots(data.root?.root_id);if($('catalog-root-dialog').open)$('catalog-root-dialog').close();return openCatalogSelection(root,'',{autoQuery:true});
}

async function selectCurrentCatalogFolder(){
  const path=state.catalogFolderPath;if(!path||state.catalogFolderActionBusy)return false;const serial=++catalogFolderRequestSerial;setCatalogFolderBrowserBusy(true,{action:true});$('catalog-folder-status').className='catalog-folder-status';$('catalog-folder-status').textContent='正在登记并检查所选文件夹…';
  try{const data=await api('/api/catalog-roots',{method:'POST',body:JSON.stringify({path,label:''})});if(serial!==catalogFolderRequestSerial||!$('catalog-folder-dialog').open)return false;closeCatalogFolderDialog(true);return await acceptCatalogRootResponse(data)}catch(error){if(serial!==catalogFolderRequestSerial||!$('catalog-folder-dialog').open)return false;$('catalog-folder-status').className='catalog-folder-status error';$('catalog-folder-status').textContent=error.message;setCatalogFolderBrowserBusy(false);return false}
}

async function registerCatalogRoot(){
  const path=$('catalog-root-path').value.trim(),label=$('catalog-root-label').value.trim();if(!path){$('catalog-root-message').textContent='请输入服务所在电脑上的绝对文件夹路径。';return}$('catalog-root-message').textContent='正在登记并检查只读数据文件夹…';$('register-catalog-root').disabled=true;
  try{await acceptCatalogRootResponse(await api('/api/catalog-roots',{method:'POST',body:JSON.stringify({path,label})}))}catch(error){$('catalog-root-message').textContent=error.message}finally{$('register-catalog-root').disabled=false}
}

async function forgetCatalogRoot(){
  const root=catalogRoot(state.catalogRootId),output=$('cat-output').value;if(!root?.removable)return;const name=catalogName(root,output);if(!confirm(`忘记“${name}”的本地数据记录？\n不会删除文件夹或其中的数据。`))return;
  try{const recent=readJsonStorage(CATALOG_RECENT_KEY,[]).filter(item=>item.root_id!==root.root_id||String(item.output||'')!==output),hasSibling=recent.some(item=>item.root_id===root.root_id);if(!hasSibling)await api(`/api/catalog-roots/${encodeURIComponent(root.root_id)}`,{method:'DELETE'});localStorage.setItem(CATALOG_RECENT_KEY,JSON.stringify(recent));localStorage.removeItem(CATALOG_SELECTION_KEY);await loadCatalogRoots();showCatalogEmpty('已忘记该数据记录，原始文件没有被删除。')}catch(error){setCatalogStatus('invalid',error.message)}
}

async function scanCatalogChildren(){
  const root=catalogRoot(state.catalogRootId),generation=state.catalogGeneration;if(!root)return;$('scan-catalog-children').disabled=true;$('catalog-discovery-message').textContent='正在受限扫描一级子目录…';$('catalog-candidate-list').innerHTML='';
  try{const data=await api('/api/catalog-candidates?'+new URLSearchParams({root_id:root.root_id}));if(generation!==state.catalogGeneration)return;const items=data.candidates||[];if(!items.length){$('catalog-discovery-message').textContent=`已检查 ${data.directories_scanned||0} 个一级子目录，没有发现 catalog.parquet。${data.truncated?'扫描已达到安全上限。':''}`;return}$('catalog-discovery-message').textContent=`发现 ${items.length} 个数据立方体${data.truncated?'（结果已达到安全上限）':''}，请选择一个打开。`;$('catalog-candidate-list').innerHTML=items.map(item=>`<button type="button" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.catalog)}">📁 ${escapeHtml(item.name)}</button>`).join('');document.querySelectorAll('#catalog-candidate-list button').forEach(button=>button.onclick=()=>openCatalogSelection(root,button.dataset.path,{autoQuery:true}))}catch(error){if(generation===state.catalogGeneration)$('catalog-discovery-message').textContent=error.message}finally{if(generation===state.catalogGeneration)$('scan-catalog-children').disabled=false}
}

async function browseDirectory(path='',silent=false){
  try{
    const data=await api('/api/output-directories?'+new URLSearchParams({path,mode:'output'}));state.directory=data.path;state.directoryPurpose='output';
    $('directory-dialog-title').textContent='选择输出目录';
    $('directory-dialog-hint').firstChild.textContent='服务器输出根：';
    $('output-root').textContent=data.root;$('dir-current').textContent=data.path||'/';$('dir-up').disabled=data.parent===null;$('dir-up').dataset.parent=data.parent??'';
    $('new-folder-row').hidden=false;$('select-output').textContent='选择当前目录';$('select-output').disabled=false;
    $('dir-list').innerHTML=data.directories.length?data.directories.map(item=>`<button data-path="${escapeHtml(item.path)}">📁 ${escapeHtml(item.name)}</button>`).join(''):'<div class="empty">这里还没有子目录</div>';
    document.querySelectorAll('#dir-list button').forEach(el=>el.onclick=()=>browseDirectory(el.dataset.path));return true;
  }catch(error){if(!silent)alert(error.message);return false}
}

async function openDirectoryBrowser(initial=''){
  state.directoryPurpose='output';let opened=await browseDirectory(initial,true);
  if(!opened&&initial)opened=await browseDirectory('');if(opened)$('output-dialog').showModal();
}
async function createFolder(){const name=$('new-folder').value.trim();if(!name)return;try{await api('/api/output-directories',{method:'POST',body:JSON.stringify({parent:state.directory,name})});$('new-folder').value='';await browseDirectory(state.directory)}catch(error){alert(error.message)}}

function setCatalogStatus(kind,message){const box=$('catalog-status');box.className=`catalog-status ${kind}`;box.textContent=message}

async function inspectCatalog(output,generation=state.catalogGeneration){
  const schema=state.capabilities?.catalog_schema_version??8;
  state.catalog=null;$('catalog-ready-tools').hidden=true;$('query-catalog').disabled=true;$('report-catalog').disabled=true;$('catalog-report').hidden=true;clearCatalogMap();setCatalogStatus('checking',`正在检查 catalog.parquet 与 Schema v${schema} 契约…`);
  try{
    const rootId=state.catalogRootId,data=await api('/api/catalog/inspect?'+new URLSearchParams({root_id:rootId,output}));if(generation!==state.catalogGeneration||rootId!==state.catalogRootId)return false;state.catalog=data;
    if(data.valid){
      const versions=(data.schema_versions||[]).join(',')||'未知';setCatalogStatus('valid',`已打开 · Schema v${versions} · ${data.record_count} 条记录 · ${data.tile_count||0} 个瓦片`);
      $('catalog-ready-tools').hidden=false;$('catalog-discovery').hidden=true;rememberCatalogSelection(state.catalogRootId,data.output??output);$('query-catalog').disabled=false;$('report-catalog').disabled=false;
    }else setCatalogStatus('invalid',`无法打开：${(data.issues||['Catalog 校验失败']).slice(0,3).join('；')}`);
    return data.valid;
  }catch(error){if(generation===state.catalogGeneration)setCatalogStatus('invalid',error.message);return false}
}

function catalogParams(tileOverride=null){
  return new URLSearchParams({root_id:state.catalogRootId,output:$('cat-output').value,tile:tileOverride??$('cat-tile').value,product:$('cat-product').value,direction:$('cat-direction').value,month:$('cat-month').value,status:$('cat-status').value});
}

function clearCatalogMap(){
  state.catalogCoverage=null;state.catalogSelectedTile=null;currentCatalogLayers.clear();if(catalogCoverageLayer)catalogCoverageLayer.clearLayers();$('toggle-local-data').hidden=true;$('legend-local-data').hidden=true;$('clear-catalog-map').disabled=true;
}

function renderCatalogCoverage(data){
  clearCatalogMap();state.catalogCoverage=data;
  if(data.truncated){$('catalog-message').textContent=`命中 ${data.tile_count} 个瓦片，超过地图单次显示上限 ${data.max_features}；请增加瓦片、产品、轨道或月份条件。`;return}
  const features=data.features?.features||[];
  if(!features.length){$('catalog-message').textContent=`没有符合条件的本地数据记录${data.missing_tiles?.length?`；${data.missing_tiles.length} 个编号未在 MGRS 字典中找到`:''}`;return}
  state.catalogCoverage=data;state.catalogCoverageVisible=true;catalogCoverageLayer.addData(data.features);if(!map.hasLayer(catalogCoverageLayer))catalogCoverageLayer.addTo(map);
  $('toggle-local-data').hidden=false;$('toggle-local-data').classList.add('active');$('toggle-local-data').setAttribute('aria-pressed','true');$('legend-local-data').hidden=false;$('clear-catalog-map').disabled=false;
  const range=data.date_range||[null,null];$('catalog-message').textContent=`命中 ${data.mapped_tile_count} 个瓦片 · ${data.total_records} 条记录 · ${range[0]?String(range[0]).slice(0,10):'无时间'} 至 ${range[1]?String(range[1]).slice(0,10):'无时间'} · 已显示在地图上`;
  const bounds=catalogCoverageLayer.getBounds();if(bounds.isValid())map.fitBounds(bounds,{padding:[30,30],maxZoom:8});showMapStatus(`已显示 ${data.mapped_tile_count} 个本地数据格网`);
}

async function queryCatalog(generation=state.catalogGeneration){
  if(!state.catalog?.valid){$('catalog-message').textContent='请先选择并打开通过校验的数据立方体目录。';return}
  const rootId=state.catalogRootId,output=$('cat-output').value,params=catalogParams();$('catalog-message').textContent='正在聚合本地记录并连接 MGRS 空间字典…';$('query-catalog').disabled=true;
  try{
    const data=await api('/api/catalog/map?'+params);if(generation!==state.catalogGeneration||rootId!==state.catalogRootId||output!==$('cat-output').value)return;renderCatalogCoverage(data);
  }catch(error){if(generation===state.catalogGeneration){clearCatalogMap();$('catalog-message').textContent=error.message}}
  finally{if(generation===state.catalogGeneration)$('query-catalog').disabled=!state.catalog?.valid}
}

async function openCatalogTileDetails(tileId){
  if(!state.catalog?.valid)return;const generation=state.catalogGeneration,rootId=state.catalogRootId;state.catalogSelectedTile=tileId;$('catalog-detail-title').textContent=`本地数据详情 · ${tileId}`;$('catalog-detail-meta').textContent='正在按需读取该瓦片记录…';$('catalog-detail-body').innerHTML='';if(!$('catalog-detail-dialog').open)$('catalog-detail-dialog').showModal();
  try{
    const data=await api('/api/catalog?'+catalogParams(tileId));if(generation!==state.catalogGeneration||rootId!==state.catalogRootId)return;$('catalog-detail-meta').textContent=`共 ${data.total} 条，当前显示 ${data.returned} 条 · ${data.catalog}`;
    $('catalog-detail-body').innerHTML=data.records.map(row=>`<tr><td>${escapeHtml(row.item_id)}</td><td>${escapeHtml(row.product_type)}</td><td>${escapeHtml(row.flight_direction||'—')}</td><td>${escapeHtml(row.datetime||row.month||'—')}</td><td><code>${escapeHtml(row.grid_id)}</code></td><td class="path" title="${escapeHtml(row.zarr_path)}">${escapeHtml(row.zarr_path||'—')}</td><td>${escapeHtml(row.status)}</td></tr>`).join('');
  }catch(error){if(generation===state.catalogGeneration)$('catalog-detail-meta').textContent=error.message}
}

function renderCountList(title,values){const entries=Object.entries(values||{});return `<div class="report-list"><b>${escapeHtml(title)}</b>${entries.length?entries.map(([key,value])=>`<div>${escapeHtml(key)}：${value}</div>`).join(''):'<div>无</div>'}</div>`}

async function generateCatalogReport(){
  if(!state.catalog?.valid)return;const generation=state.catalogGeneration,rootId=state.catalogRootId;$('catalog-message').textContent='正在生成覆盖与完整性报告…';$('report-catalog').disabled=true;
  try{
    const data=await api('/api/catalog/report?'+new URLSearchParams({root_id:rootId,output:$('cat-output').value}));if(generation!==state.catalogGeneration||rootId!==state.catalogRootId)return;const overall=data.overall||{},gaps=data.gaps||{},range=overall.date_range||[null,null];
    $('report-summary').innerHTML=[['记录',overall.total_records||0],['瓦片',overall.tile_count||0],['有效月份',overall.total_months||0],['存在缺月',gaps.tiles_with_gaps||0]].map(([label,value])=>`<div class="report-stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`).join('');
    $('report-counts').innerHTML=renderCountList('产品',data.counts?.products)+renderCountList('状态',data.counts?.statuses)+renderCountList('轨道',data.counts?.directions);
    $('report-gaps').innerHTML=`<div class="report-list"><div>时间范围：${escapeHtml(range[0]||'—')} 至 ${escapeHtml(range[1]||'—')}</div><div>完整组合：${gaps.tiles_complete||0}</div><div>缺月组合：${gaps.tiles_with_gaps||0}</div><div>报告瓦片行：${data.tile_rows_total||0}${data.truncated?'（页面摘要已截断）':''}</div></div>`;
    $('catalog-report').hidden=false;$('catalog-message').textContent=`报告已生成 · ${data.catalog.catalog}`;
  }catch(error){if(generation===state.catalogGeneration)$('catalog-message').textContent=error.message}
  finally{if(generation===state.catalogGeneration)$('report-catalog').disabled=!state.catalog?.valid}
}

async function bootstrap(){
  try{state.capabilities=await api('/api/capabilities');$('health').textContent=`已连接 · S1-GRiTS ${state.capabilities.version} · Schema v${state.capabilities.catalog_schema_version} · ${state.capabilities.stac_format==='geoparquet'?'GeoParquet':state.capabilities.stac_format}`;$('health').classList.add('ok')}catch(error){$('health').textContent='服务连接失败';$('health').classList.add('error')}
  try{state.selectedTiles=new Set(JSON.parse(sessionStorage.getItem('s1grits_selected_tiles')||'[]').map(value=>String(value).toUpperCase()))}catch{state.selectedTiles=new Set()}
  await loadCatalogRoots();initMap();buildTags();setWorkflow('scenes');setSelection('tiles');setWorkspaceTab(location.hash==='#catalog'?'catalog':'tasks');syncTileSelection();await restoreCatalogSelection();await loadTasks();setInterval(loadTasks,3000);
}

document.querySelectorAll('[data-workflow]').forEach(el=>el.onclick=()=>setWorkflow(el.dataset.workflow));
document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>setSelection(el.dataset.mode));
$('apply-bbox').onclick=applyBBox;$('submit').onclick=preflight;$('confirm-run').onclick=confirmTask;$('refresh-tasks').onclick=loadTasks;
$('tiles').oninput=()=>{clearTimeout(tileInputTimer);tileInputTimer=setTimeout(()=>syncTileSelection({fromText:true}),160)};$('clear-tiles').onclick=clearTiles;$('select-candidates').onclick=()=>selectCandidates(true);$('deselect-candidates').onclick=()=>selectCandidates(false);$('clear-aoi').onclick=clearAOI;
$('include-static').onchange=()=>{$('static-options').hidden=!$('include-static').checked};
$('resolution').onchange=()=>{$('resampling-note').textContent=$('resolution').value==='10'?'10 米采用 NoData 感知双线性插值；后向散射先在功率域插值，再转换为 dB。分类静态层仍采用最近邻。':'30 米采用最近邻重投影，保持现有产品兼容。'};
$('aoi-file').onchange=event=>uploadAOI([...event.target.files]);
$('phrase').oninput=updateConfirmation;$('copy-phrase').onclick=copyConfirmation;
$('browse-output').onclick=()=>openDirectoryBrowser($('output').value);
$('close-output').onclick=()=>$('output-dialog').close();$('dir-up').onclick=()=>browseDirectory($('dir-up').dataset.parent);$('create-folder').onclick=createFolder;
$('select-output').onclick=()=>{$('output').value=state.directory||'s1_cube';$('output-dialog').close()};
$('tasks-tab').onclick=()=>setWorkspaceTab('tasks');$('local-search-tab').onclick=async()=>{setWorkspaceTab('catalog');if(state.catalogRestorePending){const root=catalogRoot(state.catalogRootId),output=$('cat-output').value;await openCatalogSelection(root,output,{autoQuery:true,activateTab:false})}};
$('select-catalog-folder').onclick=openCatalogFolderDialog;$('change-catalog-folder').onclick=openCatalogFolderDialog;$('reselect-catalog-folder').onclick=openCatalogFolderDialog;$('open-manual-catalog').onclick=openManualCatalogDialog;$('catalog-folder-up').onclick=()=>loadCatalogFolders($('catalog-folder-up').dataset.path);$('retry-catalog-folders').onclick=()=>loadCatalogFolders($('retry-catalog-folders').dataset.path);$('select-current-catalog-folder').onclick=selectCurrentCatalogFolder;$('manual-catalog-folder').onclick=openManualCatalogFromBrowser;$('close-catalog-folder').onclick=()=>closeCatalogFolderDialog();$('cancel-catalog-folder').onclick=()=>closeCatalogFolderDialog();$('catalog-folder-dialog').onclick=event=>{if(event.target===$('catalog-folder-dialog'))closeCatalogFolderDialog()};$('catalog-folder-dialog').addEventListener('cancel',event=>{if(state.catalogFolderActionBusy)event.preventDefault();else catalogFolderRequestSerial++});$('catalog-folder-dialog').addEventListener('close',()=>{catalogFolderRequestSerial++;state.catalogFolderActionBusy=false});$('close-catalog-root').onclick=()=>$('catalog-root-dialog').close();$('register-catalog-root').onclick=registerCatalogRoot;$('forget-catalog-folder').onclick=forgetCatalogRoot;$('scan-catalog-children').onclick=scanCatalogChildren;$('catalog-root-dialog').onclick=event=>{if(event.target===$('catalog-root-dialog'))$('catalog-root-dialog').close()};
document.querySelectorAll('[data-basemap]').forEach(el=>el.onclick=()=>setBaseMap(el.dataset.basemap));$('query-catalog').onclick=queryCatalog;$('report-catalog').onclick=generateCatalogReport;
$('clear-catalog-map').onclick=()=>{clearCatalogMap();$('catalog-message').textContent='本地数据地图图层已清除。'};$('toggle-local-data').onclick=toggleCatalogCoverage;
$('close-catalog-detail').onclick=()=>$('catalog-detail-dialog').close();$('catalog-detail-dialog').onclick=event=>{if(event.target===$('catalog-detail-dialog'))$('catalog-detail-dialog').close()};
$('close-task-log').onclick=closeTaskLog;$('refresh-log').onclick=()=>pollTaskLog(false);$('task-log-dialog').onclick=event=>{if(event.target===$('task-log-dialog'))closeTaskLog()};
$('close-recovery').onclick=()=>{$('recovery-dialog').close();state.recovery=null};$('confirm-recovery').onclick=confirmRecovery;$('recovery-dialog').onclick=event=>{if(event.target===$('recovery-dialog')){$('recovery-dialog').close();state.recovery=null}};
$('toggle-mgrs').onclick=toggleMgrs;
$('map-error').onclick=()=>{$('map-error').hidden=true};
bootstrap();
