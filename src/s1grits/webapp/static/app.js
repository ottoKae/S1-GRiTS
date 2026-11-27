const {t, setView} = S1I18n;
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
  if(!response.ok){
    const details=body.error||body.detail||'';
    const error=new Error();
    Object.defineProperty(error,'message',{get:()=>t(response.status===401?'authenticationRequired':'requestError',{p0:response.status,p1:typeof details==='string'?details:JSON.stringify(details)})});
    throw error;
  }
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
  const tiles=[...state.selectedTiles].sort();$('tiles').value=tiles.join('\n');setView($('selected-count'),'textContent',()=>(tiles.length));scheduleTileSessionSave();
  if(bulk||fromText)refreshVisibleMgrsStyles();else if(changedTile)updateVisibleTileStyle(changedTile);
}

function setTileSelected(tileId,selected){
  const id=String(tileId).toUpperCase();if(selected)state.selectedTiles.add(id);else state.selectedTiles.delete(id);syncTileSelection({changedTile:id});
}

function toggleTile(tileId){
  const id=String(tileId).toUpperCase();
  if(state.aoi&&!state.candidateTiles.has(id)){setMgrsStatus(()=>t("{p0} 不在当前 AOI 候选范围；先清除 AOI 才能选择", {p0: id}),true);return}
  setTileSelected(id,!state.selectedTiles.has(id));
  setMgrsStatus(()=>`${state.selectedTiles.has(id)?t("已选择"):t("已取消")} ${id}`);if(map)map.closePopup();
}

function clearTiles(){state.selectedTiles.clear();syncTileSelection({bulk:true})}
function selectCandidates(selected=true){state.candidateTiles.forEach(id=>selected?state.selectedTiles.add(id):state.selectedTiles.delete(id));syncTileSelection({bulk:true})}
function clearAOI(){state.aoi=null;state.aoiSource='';state.candidateTiles.clear();$('aoi-file').value='';setView($('aoi-status'),'textContent',()=>(t("当前未设置 AOI。")));$('select-candidates').disabled=true;$('deselect-candidates').disabled=true;$('clear-aoi').disabled=true;drawAOI();refreshVisibleMgrsStyles()}

function showMapError(message,autoHideMs=0){
  const box=$('map-error');clearTimeout(mapNoticeTimer);setView(box,'textContent',()=>(typeof message==='function'?message():message));box.hidden=false;
  if(autoHideMs>0)mapNoticeTimer=setTimeout(()=>{box.hidden=true},autoHideMs);
}

function setMgrsWarning(message=''){
  const button=$('toggle-mgrs');button.classList.toggle('warning',Boolean(message));
  setView(button,'title',()=>((typeof message==='function'?message():message)||t("缩放至4级后显示MGRS格网")));
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
  setTileSelected(tileId,true);setMgrsStatus(()=>t("已选择 {p0}", {p0: tileId}));if(map)map.closePopup();
}

function showMapStatus(message='',kind='info'){
  const box=$('map-status');setView(box,'textContent',()=>(typeof message==='function'?message():message));box.className=`map-status ${kind}`;box.hidden=!message;
}

function bindMgrsFeature(feature,layer){
  const tileId=feature.properties.tile_id,epsg=feature.properties.utm_epsg;
  currentMgrsLayers.set(tileId,layer);
  if(mgrsLabelsEnabled)layer.bindTooltip(tileId,{sticky:!mgrsPermanentLabels,permanent:mgrsPermanentLabels,direction:'center',className:'mgrs-label'});
  layer.on('click',()=>{toggleTile(tileId);setMgrsStatus(()=>`${tileId} · EPSG:${epsg} · ${state.selectedTiles.has(tileId)?t("已选择"):t("未选择")}`)});
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
  if(!state.mgrsVisible){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning();showMapStatus(()=>t("MGRS参考格网已隐藏"));return}
  const minZoom=state.capabilities?.mgrs_map?.min_zoom??4,zoom=map.getZoom();
  if(zoom<minZoom){currentMgrsLayers.clear();mgrsGridLayer.clearLayers()}
  if(!force&&viewportCovered(state.mgrsCoverage))return;
  const coverage=expandedViewport(),bbox=[coverage.west,coverage.south,coverage.east,coverage.north].map(value=>value.toFixed(6)).join(',');
  if(mgrsRequest)mgrsRequest.abort();mgrsRequest=new AbortController();const serial=++mgrsRequestSerial;
  setMgrsStatus(()=>t("正在加载MGRS格网…"));
  try{
    const data=await api(`/api/map/mgrs?bbox=${encodeURIComponent(bbox)}&zoom=${zoom}`,{signal:mgrsRequest.signal});
    if(serial!==mgrsRequestSerial)return;
    if(!data.visible){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage={...coverage,zoom};const message=()=>t("当前视窗包含 {p0} 个 MGRS 格网；缩放至 {p1} 级后显示边界", {p0: data.count, p1: minZoom});setMgrsWarning(message);showMapStatus(message,'warning');return}
    if(data.truncated){currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage={...coverage,zoom};const message=()=>t("当前视窗包含 {p0} 个 MGRS 格网，超过单次绘制上限 {p1}，请继续放大", {p0: data.count, p1: data.max_features});setMgrsWarning(message);showMapStatus(message,'warning');return}
    setMgrsWarning();mgrsLabelsEnabled=data.returned<=800;mgrsPermanentLabels=zoom>=7&&data.returned<=180;currentMgrsLayers.clear();mgrsGridLayer.clearLayers();mgrsGridLayer.addData(data.features);
    state.mgrsCoverage={...coverage,zoom};setMgrsStatus(()=>t("已显示 {p0} 个 MGRS 格网 · z{p1}", {p0: data.returned, p1: zoom}));
  }catch(error){
    if(error.name==='AbortError')return;
    state.mgrsCoverage=null;setMgrsStatus(()=>t("MGRS参考格网加载失败：{p0}", {p0: error.message}),true);
  }
}

function scheduleMgrsLoad(force=false){
  clearTimeout(mgrsLoadTimer);mgrsLoadTimer=setTimeout(()=>loadMgrsGrid(force),180);
}

const DEFAULT_BASEMAPS={
  road:[
    {name:"Google 道路",url:'https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',attribution:'&copy; Google',max_zoom:19,subdomains:['mt0','mt1','mt2','mt3']},
    {name:"OpenStreetMap（备用）",url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'&copy; OpenStreetMap contributors',max_zoom:19}
  ],
  satellite:[
    {name:"Google 卫星",url:'https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',attribution:'Imagery &copy; Google',max_zoom:19,subdomains:['mt0','mt1','mt2','mt3']},
    {name:"Esri 卫星（备用）",url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',attribution:'Tiles &copy; Esri',max_zoom:19}
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
  layer.on('tileerror',()=>{errors+=1;if(errors<4||layer!==activeBaseLayer)return;const next=index+1;if(next<providers.length){baseProviderIndex[mode]=next;showMapError(()=>t("{p0} 无法访问，正在切换到 {p1}…", {p0: t(provider.name), p1: t(providers[next].name)}),3000);activateBaseMap(mode)}else if(!loaded){showMapError(()=>t("{p0} 暂时无法访问；MGRS 与 AOI 仍可正常使用。", {p0: t(provider.name)}))}});
  activeBaseLayer=layer;layer.addTo(map);
  document.querySelectorAll('[data-basemap]').forEach(el=>{el.classList.toggle('active',el.dataset.basemap===mode);if(el.dataset.basemap===mode)setView(el,'title',()=>(t("当前：{p0}", {p0: t(provider.name)})))});
}

function catalogStyle(feature){
  const tileId=feature?.properties?.tile_id,count=Number(feature?.properties?.record_count)||0,fillOpacity=count>200 ? .38 : count>50 ? .28 : count>10 ? .20 : .12;
  if(state.selectedTiles.has(tileId))return {color:baseMode==='satellite'?'#00e5ff':'#007c91',weight:3,opacity:1,fillColor:'#a855f7',fillOpacity,interactive:true};
  return {color:baseMode==='satellite'?'#f45cff':'#8b2fc9',weight:2,opacity:.98,fillColor:baseMode==='satellite'?'#d946ef':'#8b5cf6',fillOpacity,interactive:true};
}

function bindCatalogFeature(feature,layer){
  const p=feature.properties||{},tileId=String(p.tile_id||''),statuses=Object.entries(p.status_counts||{}).map(([key,value])=>`${key} ${value}`).join(' / ')||'—';
  currentCatalogLayers.set(tileId,layer);
  layer.bindPopup(()=>t("<div class=\"catalog-popup\"><b>{p0}</b><dl><dt>记录</dt><dd>{p1} 条</dd><dt>产品</dt><dd>{p2}</dd><dt>轨道</dt><dd>{p3}</dd><dt>时间</dt><dd>{p4} 至 {p5}</dd><dt>状态</dt><dd>{p6}</dd><dt>格网</dt><dd>{p7} 个版本</dd></dl><div class=\"popup-actions\"><button type=\"button\" onclick=\"addCatalogTile('{p8}')\">加入下载选择</button><button type=\"button\" onclick=\"openCatalogTileDetails('{p9}')\">查看详细记录</button></div></div>", {p0: escapeHtml(tileId), p1: escapeHtml(p.record_count||0), p2: escapeHtml((p.product_types||[]).join(' / ')||'—'), p3: escapeHtml((p.directions||[]).join(' / ')||'—'), p4: escapeHtml(p.month_min||'—'), p5: escapeHtml(p.month_max||'—'), p6: escapeHtml(statuses), p7: escapeHtml(p.grid_count||0), p8: escapeHtml(tileId), p9: escapeHtml(tileId)}));
  layer.on('click',()=>{state.catalogSelectedTile=tileId;showMapStatus(()=>t("本地数据 {p0} · {p1} 条记录", {p0: tileId, p1: p.record_count||0}))});
}

function addCatalogTile(tileId){setSelection('tiles');setTileSelected(tileId,true);showMapStatus(()=>t("已将 {p0} 加入下载选择", {p0: tileId}))}

function toggleCatalogCoverage(){
  if(!catalogCoverageLayer||!state.catalogCoverage)return;
  state.catalogCoverageVisible=!state.catalogCoverageVisible;const button=$('toggle-local-data');button.classList.toggle('active',state.catalogCoverageVisible);button.setAttribute('aria-pressed',String(state.catalogCoverageVisible));
  if(state.catalogCoverageVisible){if(!map.hasLayer(catalogCoverageLayer))catalogCoverageLayer.addTo(map);showMapStatus(()=>t("已显示 {p0} 个本地数据格网", {p0: state.catalogCoverage.mapped_tile_count||0}))}else{if(map.hasLayer(catalogCoverageLayer))map.removeLayer(catalogCoverageLayer);showMapStatus(()=>t("本地数据覆盖已隐藏"))}
}

function initMap(){
  if(typeof L==='undefined'){showMapError(()=>t("项目内置 Leaflet 资源未加载，请重新安装 S1-GRiTS Web 组件并重启服务。"));return}
  map=L.map('map',{center:[34.5,105],zoom:4,zoomControl:true,worldCopyJump:false,preferCanvas:true,maxBounds:[[-85.051129,-180],[85.051129,180]],maxBoundsViscosity:1});
  document.dispatchEvent(new Event('s1-language-change'));
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
  if(state.mgrsVisible){if(!map.hasLayer(mgrsGridLayer))mgrsGridLayer.addTo(map);scheduleMgrsLoad(true)}else{if(mgrsRequest)mgrsRequest.abort();currentMgrsLayers.clear();mgrsGridLayer.clearLayers();state.mgrsCoverage=null;setMgrsWarning();showMapStatus(()=>t("MGRS参考格网已隐藏"))}
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
  setView($('aoi-status'),'textContent',()=>(t("{p0} · {p1} → EPSG:4326 · {p2} 个候选格网{p3}", {p0: state.aoiSource, p1: result.source_crs, p2: result.candidate_count, p3: result.candidate_count>result.max_task_tiles?t("（最终最多选择 {p0} 个）", {p0: result.max_task_tiles}):''})));
  drawAOI();
}

async function applyBBox(){
  const w=Number($('west').value),e=Number($('east').value),s=Number($('south').value),n=Number($('north').value);
  if(![w,e,s,n].every(Number.isFinite)||w>=e||s>=n||w<70||e>140||s<10||n>55){
    setView($('aoi-status'),'textContent',()=>(t("请输入中国区域内有效的西、东、南、北边界。"))); return;
  }
  const geometry={type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]};
  setView($('aoi-status'),'textContent',()=>(t("正在计算 AOI 与 MGRS 候选关系…")));
  try{applyAOIResult(await api('/api/spatial/aoi/resolve',{method:'POST',body:JSON.stringify({geometry})}))}catch(error){setView($('aoi-status'),'textContent',()=>(error.message))}
}

async function uploadAOI(files){
  if(!files.length)return;const form=new FormData();files.forEach(file=>form.append('files',file,file.name));setView($('aoi-status'),'textContent',()=>(t("正在上传、转换投影并计算候选格网…")));
  try{applyAOIResult(await api('/api/spatial/aoi/resolve',{method:'POST',body:form}))}catch(error){setView($('aoi-status'),'textContent',()=>(error.message))}
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
    spatial_filter:$('despeckle').checked?$('production-filter').value:'none',features_glcm:$('production-glcm').checked,
    monthly_method:$('production-monthly').value,intra_pass_method:$('production-mosaic').value,
    looks_directory:$('production-looks').value.trim()||null,
    static_layers:[...document.querySelectorAll('[name=static-layer]:checked')].map(el=>el.value)
  };
}

async function preflight(){
  setView($('confirm-title'),'textContent',()=>t("确认下载规划"));setView($('confirm-message'),'textContent',()=>'');
  const button=$('submit'); button.disabled=true; setView($('form-message'),'textContent',()=>(t("正在执行空间、容量与配置预检…")));
  try{
    const plan=await api('/api/plan',{method:'POST',body:JSON.stringify(payload())}); state.plan=plan; drawPlan(plan);
    const temporal=()=>t("{p0} 年 · {p1} 个月份", {p0: plan.years.join('、'), p1: plan.months.length}),products=plan.include_static?`${plan.workflow} + static`:plan.workflow;
    setView($('plan-summary'),'innerHTML',()=>(t("<dl><dt>产品</dt><dd>{p0}</dd><dt>目标格网</dt><dd>{p1} 米 · {p2}</dd><dt>瓦片</dt><dd>{p3} 个：{p4}{p5}</dd><dt>轨道</dt><dd>{p6}</dd><dt>时间</dt><dd>{p7}</dd><dt>输出</dt><dd><code>{p8}</code></dd><dt>规划估算</dt><dd>{p9} GiB</dd></dl><p class=\"hint\">{p10}</p>", {p0: escapeHtml(products), p1: plan.target_resolution, p2: plan.resampling_method==='bilinear'?t("双线性（线性功率域）"):t("最近邻"), p3: plan.tiles.length, p4: escapeHtml(plan.tiles.slice(0,18).join(', ')), p5: plan.tiles.length>18?' …':'', p6: escapeHtml(plan.directions.join(' → ')), p7: escapeHtml(temporal()), p8: escapeHtml(plan.output_dir), p9: plan.raw_gib.toFixed(3), p10: escapeHtml(t("planEstimate"))})));
    const needs=Boolean(plan.confirmation_required);$('phrase-wrap').hidden=!needs;$('phrase').value='';setView($('phrase-example'),'textContent',()=>(plan.confirmation_phrase||''));setView($('phrase-reason'),'textContent',()=>(needs?t('largeConfirmation'):''));$('confirm-run').disabled=needs;
    const bands=['VV_dB','VH_dB',...(plan.features_ratio?['Ratio']:[]),...(plan.features_rvi?['RVI']:[]),...(plan.processing_options.features_glcm?['GLCM']:[])];
    setView($('plan-strategies'),'innerHTML',()=>`<p>${t("生产可选策略")}: ${escapeHtml(plan.spatial_filter)} / ${escapeHtml(plan.monthly_method)} / ${escapeHtml(plan.intra_pass_method)}</p><p>${bands.join(', ')}</p><p>${escapeHtml(plan.result_subdir||plan.output_subdir)}</p>`);
    $('confirm-dialog').showModal(); setView($('form-message'),'textContent',()=>(t("预检通过，请核对规划。")));
  }catch(error){setView($('form-message'),'textContent',()=>(error.message))}
  finally{button.disabled=false}
}

function updateConfirmation(){const required=Boolean(state.plan?.confirmation_required);$('confirm-run').disabled=required&&$('phrase').value!==state.plan.confirmation_phrase}

async function copyConfirmation(){if(!state.plan?.confirmation_phrase)return;try{await navigator.clipboard.writeText(state.plan.confirmation_phrase)}catch{$('phrase').value=state.plan.confirmation_phrase}updateConfirmation()}

async function confirmTask(event){
  event.preventDefault(); if(!state.plan)return;
  const button=$('confirm-run'); button.disabled=true;
  try{
    const isTool=Boolean(state.plan.operation);
    const task=await api(isTool?'/api/tools/tasks':'/api/tasks',{method:'POST',body:JSON.stringify({plan_id:state.plan.plan_id,confirmation:$('phrase').value})});
    $('confirm-dialog').close(); state.plan=null; setView($('form-message'),'textContent',()=>(t("任务 {p0} 已进入队列。", {p0: task.run_id}))); await loadTasks();
    if(isTool){$('catalog-detail-dialog').close();setWorkspaceTab('tasks');}
  }catch(error){setView($('confirm-message'),'textContent',()=>error.message);setView($('form-message'),'textContent',()=>(error.message))}
  finally{button.disabled=false}
}

const statusNames={queued:"排队中",running:"处理中",detached:"外部处理中",processing:"影像处理",static:"静态图层",validating:"目录复验",associating:"关联复验",done:"已完成",failed:"失败",cancelled:"已取消",cancelling:"取消中",interrupted:"已中断"};
function taskCard(task){
  const progress=Math.max(0,Math.min(1,Number(task.progress)||0));
  const temporal=task.tool_request?t("已有产品：{p0}",{p0:(task.parent_item_ids||[task.parent_item_id]).join(', ')}):t("{p0} · {p1}月", {p0: (task.years||[]).join(','), p1: (task.months||[]).length});
  const productLabel=task.include_static?`${task.workflow} + static`:task.result_type?`${task.workflow} · ${task.result_type==='virtual'?t("虚拟视图"):['analysis','raster'].includes(task.result_type)?t("工具结果"):t("实体产品")}`:task.workflow;
  const cancel=!['done','failed','cancelled','interrupted','detached'].includes(task.status)?t("<button class=\"secondary\" onclick=\"cancelTask('{p0}')\">取消</button>", {p0: task.run_id}):'';
  const recovery=task.recoverable?t("<button class=\"primary\" onclick=\"openRecovery('{p0}')\">恢复任务</button>", {p0: task.run_id}):'';
  const association=task.validation?.static_association?t(" · Static关联 {p0}", {p0: task.validation.static_association.pairs_checked}):'';
  const validation=task.validation?t("<div class=\"task-meta ok\">Catalog {p0} 条 · {p1} 瓦片{p2}</div>", {p0: task.validation.records, p1: task.validation.tiles, p2: association}):'';
  const error=task.error?`<div class="task-meta error" title="${escapeHtml(task.error)}">${escapeHtml(task.error)}</div>`:'';
  const catalogArgument=escapeHtml(JSON.stringify(task.result_subdir||task.output_subdir||''))+','+escapeHtml(JSON.stringify(task.status==='done'?task.result_item_id||null:null))+','+escapeHtml(JSON.stringify(['analysis','raster'].includes(task.result_type)?task.run_id:null));
  return t("<article class=\"task-card\"><div class=\"task-title\"><b>{p0} · {p1} 瓦片</b><span class=\"badge {p2}\">{p3}</span></div><div class=\"task-meta\">{p4} · {p5}</div><div class=\"task-meta\" title=\"{p6}\">{p7}</div><div class=\"progress\"><i style=\"width:{p8}%\"></i></div>{p9}{p10}<div class=\"task-actions\"><button class=\"secondary\" onclick=\"openLog('{p11}')\">日志</button>{p12}{p13}<button class=\"secondary\" onclick=\"catalogFor({p14})\">检索结果</button></div></article>", {p0: escapeHtml(productLabel), p1: (task.tiles||[]).length, p2: escapeHtml(task.status), p3: t(statusNames[task.status])||t(statusNames[task.stage])||escapeHtml(task.status), p4: escapeHtml((task.directions||[]).join(' + ')), p5: escapeHtml(temporal), p6: escapeHtml(task.output_dir), p7: escapeHtml(task.output_subdir), p8: progress*100, p9: validation, p10: error, p11: task.run_id, p12: recovery, p13: cancel, p14: catalogArgument});
}

async function loadTasks(){
  try{state.tasks=await api('/api/tasks'); setView($('tasks'),'innerHTML',()=>(state.tasks.length?state.tasks.map(taskCard).join(''):''))}catch(error){setView($('tasks'),'innerHTML',()=>(`<div class="empty error">${escapeHtml(error.message)}</div>`))}
}
async function cancelTask(id){if(!confirm(t("确认取消该任务及其子进程？")))return; try{await api(`/api/tasks/${id}`,{method:'DELETE'});await loadTasks()}catch(error){alert(error.message)}}

async function openRecovery(id){
  setView($('recovery-summary'),'innerHTML',()=>(t("<p>正在检查原配置、输出目录和并发状态…</p>")));$('confirm-recovery').disabled=true;$('recovery-dialog').showModal();
  try{
    const check=await api(`/api/tasks/${encodeURIComponent(id)}/recovery`);state.recovery=check;
    const issues=(check.issues||[]).map(value=>`<li>${escapeHtml(value)}</li>`).join(''),warnings=(check.warnings||[]).map(value=>`<li>${escapeHtml(value)}</li>`).join('');
    setView($('recovery-summary'),'innerHTML',()=>(t("<p class=\"{p0}\">{p1}</p><dl><dt>输出目录</dt><dd><code>{p2}</code></dd><dt>恢复位置</dt><dd>第 {p3}/{p4} 条受控命令</dd><dt>执行次数</dt><dd>第 {p5} 次</dd><dt>现有成果</dt><dd>{p6} 个 Zarr · Catalog {p7}</dd></dl>{p8}{p9}<p class=\"hint\">恢复不会删除已有输出；完整月份将跳过，中断附近内容可能重新下载或处理。完成后会重新执行 Catalog 门禁。</p>", {p0: check.recoverable?'ok':'error', p1: escapeHtml(t(check.recoverable?"resumeReady":"resumeBlocked")), p2: escapeHtml(check.output_dir), p3: check.resume_command, p4: check.command_total, p5: check.attempt_next, p6: check.zarr_count, p7: check.catalog_exists?t("已存在"):t("尚未生成"), p8: issues?t("<h3>阻止恢复</h3><ul class=\"error\">{p0}</ul>", {p0: issues}):'', p9: warnings?t("<h3>恢复说明</h3><ul>{p0}</ul>", {p0: warnings}):''})));
    $('confirm-recovery').disabled=!check.recoverable;
  }catch(error){state.recovery=null;setView($('recovery-summary'),'innerHTML',()=>(`<p class="error">${escapeHtml(error.message)}</p>`))}
}

async function confirmRecovery(){if(!state.recovery?.recoverable)return;const button=$('confirm-recovery');button.disabled=true;try{await api(`/api/tasks/${encodeURIComponent(state.recovery.job_id)}/resume`,{method:'POST'});$('recovery-dialog').close();state.recovery=null;await loadTasks()}catch(error){setView($('recovery-summary'),'innerHTML',()=>`<p class="error">${escapeHtml(error.message)}</p>`)}finally{button.disabled=false}}
function formatBytes(value){const bytes=Number(value)||0;if(bytes<1024)return `${bytes} B`;if(bytes<1024**2)return `${(bytes/1024).toFixed(1)} KiB`;return `${(bytes/1024**2).toFixed(1)} MiB`}

function renderTaskStages(task){
  const stages=[['queued',"排队"],['processing',task.tool_request?"工具计算":"影像"]];
  if(task.include_static)stages.push(['static',"静态层"]);
  stages.push(['validating','Catalog']);if(task.include_static)stages.push(['associating',"关联"]);stages.push(['done',"完成"]);
  const current=stages.findIndex(([key])=>key===task.stage),terminal=['failed','cancelled','interrupted'].includes(task.status);
  setView($('task-stage-track'),'innerHTML',()=>(stages.map(([key,label],index)=>{
    let cls='';if(task.status==='done'||index<current)cls='done';else if(index===current)cls=terminal?'failed':'active';
    return `<span class="stage-pill ${cls}">${escapeHtml(t(label))}</span>`;
  }).join('')+(terminal?`<span class="stage-pill failed">${escapeHtml(t(statusNames[task.status])||task.status)}</span>`:'')));
}

function appendTaskEvents(events,reset=false){
  const box=$('task-events');
  if(!activeLog)return;
  activeLog.events=[...(reset?[]:activeLog.events||[]),...events].slice(-200);
  const displayed=activeLog.events;
  setView(box,'innerHTML',()=>displayed.map(event=>`<div class="task-event ${escapeHtml(event.level||'info')}"><time>${escapeHtml(event.timestamp||'')}</time><b>${escapeHtml(t(statusNames[event.stage])||event.event||t("事件"))}</b>${event.message?`<div>${escapeHtml(event.message)}</div>`:''}</div>`).join(''));
  if($('log-follow').checked)box.scrollTop=box.scrollHeight;
}

async function pollTaskLog(reset=false){
  if(!activeLog)return;const session=activeLog,id=session.id;
  if(reset){session.logOffset=0;session.eventOffset=0;$('task-log-output').textContent='';setView($('task-events'),'innerHTML',()=>(''))}
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
    setView($('task-log-title'),'textContent',()=>(t("任务日志 · {p0}", {p0: id})));
    setView($('task-log-meta'),'textContent',()=>(t("{p0} · {p1}/{p2} 条命令 · {p3}", {p0: t(statusNames[task.stage])||task.stage, p1: task.command_index||0, p2: task.command_total||0, p3: task.output_subdir})));
    setView($('log-size'),'textContent',()=>(formatBytes(logChunk.size)));renderTaskStages(task);
    clearTimeout(logPollTimer);if(!['done','failed','cancelled','interrupted'].includes(task.status))logPollTimer=setTimeout(()=>pollTaskLog(false),1500);
  }catch(error){if(activeLog===session)$('task-log-output').textContent+=t("\\n[日志读取失败] {p0}\\n", {p0: error.message})}
}

function openLog(id){
  clearTimeout(logPollTimer);activeLog={id,logOffset:0,eventOffset:0};
  setView($('task-log-title'),'textContent',()=>(t("任务日志 · {p0}", {p0: id})));setView($('task-log-meta'),'textContent',()=>(t("正在读取任务状态…")));$('download-log').href=`/api/tasks/${encodeURIComponent(id)}/log`;
  if(!$('task-log-dialog').open)$('task-log-dialog').showModal();pollTaskLog(true);
}

function closeTaskLog(){clearTimeout(logPollTimer);activeLog=null;$('task-log-dialog').close()}
function setWorkspaceTab(tab){
  state.workspaceTab=tab;const tasks=tab==='tasks';$('tasks-tab').classList.toggle('active',tasks);$('local-search-tab').classList.toggle('active',!tasks);$('tasks-tab').setAttribute('aria-selected',String(tasks));$('local-search-tab').setAttribute('aria-selected',String(!tasks));$('tasks-pane').hidden=!tasks;$('local-search-pane').hidden=tasks;$('refresh-tasks').hidden=!tasks;
}
const CATALOG_SELECTION_KEY='s1grits_catalog_selection',CATALOG_RECENT_KEY='s1grits_catalog_recent';

function catalogRoot(rootId){return state.catalogRoots.find(item=>item.root_id===rootId)||null}
function catalogPath(root,output=''){if(!root)return'';if(!output)return root.path_display;const separator=root.path_display.includes('\\')?'\\':'/';return root.path_display.replace(/[\\/]$/,'')+separator+String(output).replaceAll('/',separator)}
function catalogName(root,output=''){const part=String(output||'').split('/').filter(Boolean).at(-1);return part||(root?.root_id==='workspace'?t('serverOutput'):root?.label)||t("本地数据立方体")}
function readJsonStorage(key,fallback){try{return JSON.parse(localStorage.getItem(key)||'null')??fallback}catch{return fallback}}

function renderCatalogRecent(){
  const stored=readJsonStorage(CATALOG_RECENT_KEY,[]),known=new Set(),recent=[];
  for(const item of Array.isArray(stored)?stored:[]){const root=catalogRoot(item.root_id);if(!root||known.has(`${item.root_id}|${item.output||''}`))continue;recent.push({root_id:item.root_id,output:item.output||'',touched_at:item.touched_at||0});known.add(`${item.root_id}|${item.output||''}`)}
  state.catalogRecent=recent.slice(0,8);const wrap=$('recent-catalogs'),list=$('recent-catalog-list');wrap.hidden=!state.catalogRecent.length;
  setView(list,'innerHTML',()=>(state.catalogRecent.map(item=>{const root=catalogRoot(item.root_id),path=catalogPath(root,item.output);return `<button type="button" data-root-id="${escapeHtml(item.root_id)}" data-output="${escapeHtml(item.output)}" title="${escapeHtml(path)}">📁 ${escapeHtml(catalogName(root,item.output))} · ${escapeHtml(path)}</button>`}).join('')));
  document.querySelectorAll('#recent-catalog-list button').forEach(button=>button.onclick=()=>openCatalogSelection(catalogRoot(button.dataset.rootId),button.dataset.output||''));
}

function rememberCatalogSelection(rootId,output){
  const entry={root_id:rootId,output:output||'',touched_at:Date.now()},stored=readJsonStorage(CATALOG_RECENT_KEY,[]),recent=[entry,...(Array.isArray(stored)?stored:[]).filter(item=>item.root_id!==rootId||String(item.output||'')!==entry.output)].slice(0,8);
  localStorage.setItem(CATALOG_SELECTION_KEY,JSON.stringify(entry));localStorage.setItem(CATALOG_RECENT_KEY,JSON.stringify(recent));renderCatalogRecent();
}

function showCatalogEmpty(message=''){
  state.catalogGeneration++;state.catalogRestorePending=false;state.catalog=null;state.catalogRootId='';$('cat-output').value='';$('catalog-empty').hidden=false;$('catalog-selection').hidden=true;$('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=true;$('catalog-report').hidden=true;setView($('catalog-home-message'),'textContent',()=>(typeof message==='function'?message():message));$('query-catalog').disabled=true;$('report-catalog').disabled=true;if($('catalog-detail-dialog').open)$('catalog-detail-dialog').close();clearCatalogMap();
}

function resetCatalogFilters(){$('cat-tile').value='';$('cat-product').value='';$('cat-direction').value='';$('cat-month').value='';$('cat-status').value=''}

function prepareCatalogSelection(root,output=''){
  if(!root){showCatalogEmpty(()=>t("所选文件夹记录已经失效，请重新选择。"));return false}state.catalogGeneration++;state.catalogRestorePending=false;state.catalogRootId=root.root_id;$('cat-output').value=output||'';state.catalog=null;resetCatalogFilters();$('catalog-empty').hidden=true;$('catalog-selection').hidden=false;$('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=true;$('catalog-report').hidden=true;setView($('catalog-candidate-list'),'innerHTML',()=>(''));setView($('catalog-selection-name'),'textContent',()=>(catalogName(root,output)));setView($('catalog-selection-path'),'textContent',()=>(catalogPath(root,output)));setView($('catalog-selection-path'),'title',()=>(catalogPath(root,output)));$('forget-catalog-folder').hidden=!root.removable;$('query-catalog').disabled=true;$('report-catalog').disabled=true;setView($('catalog-message'),'textContent',()=>(''));if($('catalog-detail-dialog').open)$('catalog-detail-dialog').close();clearCatalogMap();setCatalogStatus('checking',()=>t("正在检查 catalog.parquet 与数据结构…"));return true;
}

function showCatalogDiscovery(){
  $('catalog-ready-tools').hidden=true;$('catalog-discovery').hidden=false;setView($('catalog-candidate-list'),'innerHTML',()=>(''));setView($('catalog-discovery-message'),'textContent',()=>(t("所选目录根部不存在 catalog.parquet。可以重新选择，或仅检查当前目录的一级子目录。")));setCatalogStatus('invalid',()=>t("当前文件夹不是可直接打开的数据立方体"));
}

async function loadCatalogRoots(preferredRootId=null){
  try{const data=await api('/api/catalog-roots');state.catalogRoots=data.roots||[];renderCatalogRecent();return catalogRoot(preferredRootId)}catch(error){state.catalogRoots=[];renderCatalogRecent();showCatalogEmpty(()=>t("本地数据目录加载失败：{p0}", {p0: error.message}));return null}
}

async function openCatalogSelection(root,output='',{autoQuery=true,activateTab=true}={}){
  if(!prepareCatalogSelection(root,output))return false;const generation=state.catalogGeneration;if(activateTab)setWorkspaceTab('catalog');const valid=await inspectCatalog(output,generation);
  if(valid){if(autoQuery)await queryCatalog(generation);return true}
  if((state.catalog?.issues||[]).some(issue=>String(issue).includes('不存在 catalog.parquet')))showCatalogDiscovery();return false;
}

async function restoreCatalogSelection(){
  const saved=readJsonStorage(CATALOG_SELECTION_KEY,null),root=saved?catalogRoot(saved.root_id):null;if(!root){showCatalogEmpty();return}if(location.hash==='#catalog'){await openCatalogSelection(root,saved.output||'',{autoQuery:true,activateTab:true});return}prepareCatalogSelection(root,saved.output||'');state.catalogRestorePending=true;setCatalogStatus('neutral',()=>t("上次使用的数据已恢复；进入“本地数据”时将重新校验。"));
}

async function catalogFor(output,itemId=null,fileJobId=null){
  if(fileJobId)return openToolResult(fileJobId);
  if(!state.catalogRoots.length)await loadCatalogRoots();const root=catalogRoot('workspace');setWorkspaceTab('catalog');await openCatalogSelection(root,output||'',{autoQuery:true});
  if(itemId){await openCatalogTileDetails(null);await selectCatalogProduct(itemId);}
}

function openManualCatalogDialog(){
  $('catalog-root-path').value='';$('catalog-root-label').value='';setView($('catalog-root-message'),'textContent',()=>(''));$('catalog-root-dialog').showModal();
}

function setCatalogFolderBrowserBusy(busy,{action=false}={}){
  state.catalogFolderActionBusy=Boolean(busy&&action);$('catalog-folder-list').setAttribute('aria-busy',String(busy));$('catalog-folder-up').disabled=busy||state.catalogFolderParent===null;$('select-current-catalog-folder').disabled=busy||!state.catalogFolderPath;$('retry-catalog-folders').disabled=busy;$('manual-catalog-folder').disabled=busy;$('close-catalog-folder').disabled=state.catalogFolderActionBusy;$('cancel-catalog-folder').disabled=state.catalogFolderActionBusy;document.querySelectorAll('#catalog-folder-list button').forEach(button=>{button.disabled=busy});
}

function closeCatalogFolderDialog(force=false){
  if(state.catalogFolderActionBusy&&!force)return;catalogFolderRequestSerial++;state.catalogFolderActionBusy=false;const dialog=$('catalog-folder-dialog');if(dialog.open)dialog.close();
}

function folderBrowserMessage(message){setView($('catalog-folder-list'),'innerHTML',()=>(`<div class="folder-browser-message">${escapeHtml(typeof message==='function'?message():message)}</div>`))}

async function loadCatalogFolders(path=''){
  const dialog=$('catalog-folder-dialog'),requested=String(path??''),serial=++catalogFolderRequestSerial;if(!dialog.open)return false;setCatalogFolderBrowserBusy(true);$('catalog-folder-status').className='catalog-folder-status';setView($('catalog-folder-status'),'textContent',()=>(requested?t("正在打开 {p0}…", {p0: requested}):t("正在读取本机盘符…")));$('retry-catalog-folders').hidden=true;folderBrowserMessage(()=>t("正在加载文件夹…"));
  try{
    const data=await api('/api/catalog-folders?'+new URLSearchParams({path:requested}));if(serial!==catalogFolderRequestSerial||!dialog.open)return false;const directoryItems=Array.isArray(data.directories)?data.directories:[],driveItems=Array.isArray(data.drives)?data.drives:[],directories=(data.mode==='drives'||(!requested&&driveItems.length)?driveItems:directoryItems).map(item=>({...item,has_catalog:Boolean(item.has_catalog??item.catalog_available)}));state.catalogFolderPath=String(data.current_path??data.path??requested??'');const parentValue=data.parent_path??data.parent;state.catalogFolderParent=parentValue===null||parentValue===undefined?null:String(parentValue);const current=()=>state.catalogFolderPath||t("此电脑");setView($('catalog-folder-current'),'textContent',()=>(current()));setView($('catalog-folder-current'),'title',()=>(current()));$('catalog-folder-up').dataset.path=state.catalogFolderParent??'';
    setView($('catalog-folder-list'),'innerHTML',()=>(directories.length?directories.map(item=>t("<div class=\"catalog-folder-row\" role=\"listitem\"><button class=\"catalog-folder-entry\" type=\"button\" data-path=\"{p0}\" aria-label=\"进入文件夹 {p1}\"><span class=\"folder-icon\" aria-hidden=\"true\">📁</span><span class=\"folder-name\">{p2}</span>{p3}<span class=\"folder-enter\" aria-hidden=\"true\">进入 ›</span></button></div>", {p0: escapeHtml(item.path), p1: escapeHtml(item.name), p2: escapeHtml(item.name), p3: item.has_catalog?t("<span class=\"catalog-ready\">含 Catalog</span>"):''})).join(''):t("<div class=\"folder-browser-message\">当前目录没有可进入的子文件夹。</div>")));document.querySelectorAll('#catalog-folder-list .catalog-folder-entry').forEach(button=>button.onclick=()=>loadCatalogFolders(button.dataset.path));const catalogCount=directories.filter(item=>item.has_catalog).length;setView($('catalog-folder-status'),'textContent',()=>(state.catalogFolderPath?t("发现 {p0} 个子文件夹{p1}。", {p0: directories.length, p1: catalogCount?t("，其中 {p0} 个包含 Catalog", {p0: catalogCount}):''}):t("请选择一个盘符或可用位置。")));setCatalogFolderBrowserBusy(false);return true;
  }catch(error){
    if(serial!==catalogFolderRequestSerial||!dialog.open)return false;$('catalog-folder-status').className='catalog-folder-status error';setView($('catalog-folder-status'),'textContent',()=>(t("文件夹加载失败：{p0}", {p0: error.message})));$('retry-catalog-folders').dataset.path=requested;$('retry-catalog-folders').hidden=false;folderBrowserMessage(()=>t("无法读取该位置。请重试，或使用下方其他方式。"));setCatalogFolderBrowserBusy(false);$('select-current-catalog-folder').disabled=true;return false;
  }
}

function openCatalogFolderDialog(){
  const dialog=$('catalog-folder-dialog');state.catalogFolderPath='';state.catalogFolderParent=null;state.catalogFolderActionBusy=false;setView($('catalog-folder-current'),'textContent',()=>(t("此电脑")));setView($('catalog-folder-current'),'title',()=>(t("此电脑")));if(!dialog.open)dialog.showModal();loadCatalogFolders('');
}

function openManualCatalogFromBrowser(){closeCatalogFolderDialog(true);openManualCatalogDialog()}

async function acceptCatalogRootResponse(data){
  if(data.cancelled){const message=()=>t("已取消选择，当前本地数据未改变。");if($('catalog-empty').hidden)setView($('catalog-message'),'textContent',()=>(typeof message==='function'?message():message));else setView($('catalog-home-message'),'textContent',()=>(typeof message==='function'?message():message));return false}const root=await loadCatalogRoots(data.root?.root_id);if($('catalog-root-dialog').open)$('catalog-root-dialog').close();return openCatalogSelection(root,'',{autoQuery:true});
}

async function selectCurrentCatalogFolder(){
  const path=state.catalogFolderPath;if(!path||state.catalogFolderActionBusy)return false;const serial=++catalogFolderRequestSerial;setCatalogFolderBrowserBusy(true,{action:true});$('catalog-folder-status').className='catalog-folder-status';setView($('catalog-folder-status'),'textContent',()=>(t("正在登记并检查所选文件夹…")));
  try{const data=await api('/api/catalog-roots',{method:'POST',body:JSON.stringify({path,label:''})});if(serial!==catalogFolderRequestSerial||!$('catalog-folder-dialog').open)return false;closeCatalogFolderDialog(true);return await acceptCatalogRootResponse(data)}catch(error){if(serial!==catalogFolderRequestSerial||!$('catalog-folder-dialog').open)return false;$('catalog-folder-status').className='catalog-folder-status error';setView($('catalog-folder-status'),'textContent',()=>(error.message));setCatalogFolderBrowserBusy(false);return false}
}

async function registerCatalogRoot(){
  const path=$('catalog-root-path').value.trim(),label=$('catalog-root-label').value.trim();if(!path){setView($('catalog-root-message'),'textContent',()=>(t("请输入服务所在电脑上的绝对文件夹路径。")));return}setView($('catalog-root-message'),'textContent',()=>(t("正在登记并检查只读数据文件夹…")));$('register-catalog-root').disabled=true;
  try{await acceptCatalogRootResponse(await api('/api/catalog-roots',{method:'POST',body:JSON.stringify({path,label})}))}catch(error){setView($('catalog-root-message'),'textContent',()=>(error.message))}finally{$('register-catalog-root').disabled=false}
}

async function forgetCatalogRoot(){
  const root=catalogRoot(state.catalogRootId),output=$('cat-output').value;if(!root?.removable)return;const name=catalogName(root,output);if(!confirm(t("忘记“{p0}”的本地数据记录？\\n不会删除文件夹或其中的数据。", {p0: name})))return;
  try{const recent=readJsonStorage(CATALOG_RECENT_KEY,[]).filter(item=>item.root_id!==root.root_id||String(item.output||'')!==output),hasSibling=recent.some(item=>item.root_id===root.root_id);if(!hasSibling)await api(`/api/catalog-roots/${encodeURIComponent(root.root_id)}`,{method:'DELETE'});localStorage.setItem(CATALOG_RECENT_KEY,JSON.stringify(recent));localStorage.removeItem(CATALOG_SELECTION_KEY);await loadCatalogRoots();showCatalogEmpty(()=>t("已忘记该数据记录，原始文件没有被删除。"))}catch(error){setCatalogStatus('invalid',()=>error.message)}
}

async function scanCatalogChildren(){
  const root=catalogRoot(state.catalogRootId),generation=state.catalogGeneration;if(!root)return;$('scan-catalog-children').disabled=true;setView($('catalog-discovery-message'),'textContent',()=>(t("正在受限扫描一级子目录…")));setView($('catalog-candidate-list'),'innerHTML',()=>(''));
  try{const data=await api('/api/catalog-candidates?'+new URLSearchParams({root_id:root.root_id}));if(generation!==state.catalogGeneration)return;const items=data.candidates||[];if(!items.length){setView($('catalog-discovery-message'),'textContent',()=>(t("已检查 {p0} 个一级子目录，没有发现 catalog.parquet。{p1}", {p0: data.directories_scanned||0, p1: data.truncated?t("扫描已达到安全上限。"):''})));return}setView($('catalog-discovery-message'),'textContent',()=>(t("发现 {p0} 个数据立方体{p1}，请选择一个打开。", {p0: items.length, p1: data.truncated?t("（结果已达到安全上限）"):''})));setView($('catalog-candidate-list'),'innerHTML',()=>(items.map(item=>`<button type="button" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.catalog)}">📁 ${escapeHtml(item.name)}</button>`).join('')));document.querySelectorAll('#catalog-candidate-list button').forEach(button=>button.onclick=()=>openCatalogSelection(root,button.dataset.path,{autoQuery:true}))}catch(error){if(generation===state.catalogGeneration)setView($('catalog-discovery-message'),'textContent',()=>(error.message))}finally{if(generation===state.catalogGeneration)$('scan-catalog-children').disabled=false}
}

async function browseDirectory(path='',silent=false){
  try{
    const data=await api('/api/output-directories?'+new URLSearchParams({path,mode:'output'}));state.directory=data.path;state.directoryPurpose='output';
    setView($('directory-dialog-title'),'textContent',()=>(t("选择输出目录")));
    setView($('directory-dialog-hint').querySelector('[data-i18n]'),'textContent',()=>(t("服务器输出根：")));
    setView($('output-root'),'textContent',()=>(data.root));setView($('dir-current'),'textContent',()=>(data.path||'/'));$('dir-up').disabled=data.parent===null;$('dir-up').dataset.parent=data.parent??'';
    $('new-folder-row').hidden=false;setView($('select-output'),'textContent',()=>(t("选择当前目录")));$('select-output').disabled=false;
    setView($('dir-list'),'innerHTML',()=>(data.directories.length?data.directories.map(item=>`<button data-path="${escapeHtml(item.path)}">📁 ${escapeHtml(item.name)}</button>`).join(''):t("<div class=\"empty\">这里还没有子目录</div>")));
    document.querySelectorAll('#dir-list button').forEach(el=>el.onclick=()=>browseDirectory(el.dataset.path));return true;
  }catch(error){if(!silent)alert(error.message);return false}
}

async function openDirectoryBrowser(initial=''){
  state.directoryPurpose='output';let opened=await browseDirectory(initial,true);
  if(!opened&&initial)opened=await browseDirectory('');if(opened)$('output-dialog').showModal();
}
async function createFolder(){const name=$('new-folder').value.trim();if(!name)return;try{await api('/api/output-directories',{method:'POST',body:JSON.stringify({parent:state.directory,name})});$('new-folder').value='';await browseDirectory(state.directory)}catch(error){alert(error.message)}}

function setCatalogStatus(kind,message){const box=$('catalog-status');box.className=`catalog-status ${kind}`;setView(box,'textContent',()=>(typeof message==='function'?message():message))}

async function inspectCatalog(output,generation=state.catalogGeneration){
  const schema=state.capabilities?.catalog_schema_version??8;
  state.catalog=null;$('catalog-ready-tools').hidden=true;$('query-catalog').disabled=true;$('report-catalog').disabled=true;$('catalog-report').hidden=true;clearCatalogMap();setCatalogStatus('checking',()=>t("正在检查 catalog.parquet 与 Schema v{p0} 契约…", {p0: schema}));
  try{
    const rootId=state.catalogRootId,data=await api('/api/catalog/inspect?'+new URLSearchParams({root_id:rootId,output}));if(generation!==state.catalogGeneration||rootId!==state.catalogRootId)return false;state.catalog=data;
    if(data.valid){
      const versions=(data.schema_versions||[]).join(',')||'';setCatalogStatus('valid',()=>t("已打开 · Schema v{p0} · {p1} 条记录 · {p2} 个瓦片", {p0: versions||t("未知"), p1: data.record_count, p2: data.tile_count||0}));
      $('catalog-ready-tools').hidden=false;$('catalog-discovery').hidden=true;rememberCatalogSelection(state.catalogRootId,data.output??output);$('query-catalog').disabled=false;$('report-catalog').disabled=false;
    }else setCatalogStatus('invalid',()=>t("无法打开：{p0}", {p0: (data.issues||[t("Catalog 校验失败")]).slice(0,3).join('；')}));
    return data.valid;
  }catch(error){if(generation===state.catalogGeneration)setCatalogStatus('invalid',()=>error.message);return false}
}

function catalogParams(tileOverride=null){
  const params=new URLSearchParams({root_id:state.catalogRootId,output:$('cat-output').value,tile:tileOverride??$('cat-tile').value,product:$('cat-product').value,direction:$('cat-direction').value,month:$('cat-month').value,status:$('cat-status').value});
  for(const [key,id] of [['track','cat-track'],['processing_level','cat-level'],['processing_signature','cat-signature'],['product_variant','cat-variant'],['representation','cat-representation']]){
    if($(id).value.trim())params.set(key,$(id).value.trim());
  }
  return params;
}

function clearCatalogMap(){
  state.catalogCoverage=null;state.catalogSelectedTile=null;currentCatalogLayers.clear();if(catalogCoverageLayer)catalogCoverageLayer.clearLayers();$('toggle-local-data').hidden=true;$('legend-local-data').hidden=true;$('clear-catalog-map').disabled=true;
}

function renderCatalogCoverage(data){
  clearCatalogMap();state.catalogCoverage=data;
  if(data.truncated){setView($('catalog-message'),'textContent',()=>(t("命中 {p0} 个瓦片，超过地图单次显示上限 {p1}；请增加瓦片、产品、轨道或月份条件。", {p0: data.tile_count, p1: data.max_features})));return}
  const features=data.features?.features||[];
  if(!features.length){setView($('catalog-message'),'textContent',()=>(t("没有符合条件的本地数据记录{p0}", {p0: data.missing_tiles?.length?t("；{p0} 个编号未在 MGRS 字典中找到", {p0: data.missing_tiles.length}):''})));return}
  state.catalogCoverage=data;state.catalogCoverageVisible=true;catalogCoverageLayer.addData(data.features);if(!map.hasLayer(catalogCoverageLayer))catalogCoverageLayer.addTo(map);
  $('toggle-local-data').hidden=false;$('toggle-local-data').classList.add('active');$('toggle-local-data').setAttribute('aria-pressed','true');$('legend-local-data').hidden=false;$('clear-catalog-map').disabled=false;
  const range=data.date_range||[null,null];setView($('catalog-message'),'textContent',()=>(t("命中 {p0} 个瓦片 · {p1} 条记录 · {p2} 至 {p3} · 已显示在地图上", {p0: data.mapped_tile_count, p1: data.total_records, p2: range[0]?String(range[0]).slice(0,10):t("无时间"), p3: range[1]?String(range[1]).slice(0,10):t("无时间")})));
  const bounds=catalogCoverageLayer.getBounds();if(bounds.isValid())map.fitBounds(bounds,{padding:[30,30],maxZoom:8});showMapStatus(()=>t("已显示 {p0} 个本地数据格网", {p0: data.mapped_tile_count}));
}

async function queryCatalog(generation=state.catalogGeneration){
  if(!state.catalog?.valid){setView($('catalog-message'),'textContent',()=>(t("请先选择并打开通过校验的数据立方体目录。")));return}
  const rootId=state.catalogRootId,output=$('cat-output').value,params=catalogParams();setView($('catalog-message'),'textContent',()=>(t("正在聚合本地记录并连接 MGRS 空间字典…")));$('query-catalog').disabled=true;
  try{
    const data=await api('/api/catalog/map?'+params);if(generation!==state.catalogGeneration||rootId!==state.catalogRootId||output!==$('cat-output').value)return;renderCatalogCoverage(data);
  }catch(error){if(generation===state.catalogGeneration){clearCatalogMap();setView($('catalog-message'),'textContent',()=>(error.message))}}
  finally{if(generation===state.catalogGeneration)$('query-catalog').disabled=!state.catalog?.valid}
}

let catalogDetailSerial=0,productReadSerial=0,catalogDetailParams=null,catalogDetailOffset=0,selectedProduct=null;
async function openCatalogTileDetails(tileId,offset=0){
  if(!state.catalog?.valid)return;
  const generation=state.catalogGeneration,serial=++catalogDetailSerial;
  productReadSerial++;selectedProduct=null;$('product-reader').hidden=true;
  state.catalogSelectedTile=tileId;catalogDetailOffset=offset;
  if(offset===0)catalogDetailParams=catalogParams(tileId);
  const mosaicContext=JSON.stringify([catalogDetailParams.get('root_id'),catalogDetailParams.get('output')]);
  if($('mosaic-parents').dataset.context!==mosaicContext){
    $('mosaic-parents').dataset.context=mosaicContext;$('mosaic-parents').value='';
    $('mosaic-output').value='analysis/mosaic-'+Date.now();$('mosaic-crs').value='';$('mosaic-bounds').value='';
  }
  const mosaicAvailable=Boolean(state.capabilities?.tools?.local?.mosaic_grid?.available);
  $('plan-mosaic').disabled=!mosaicAvailable;
  setView($('mosaic-message'),'textContent',()=>mosaicAvailable?'':t("需要 GDAL 的 VRT 和 COG 驱动。"));
  const params=new URLSearchParams(catalogDetailParams);params.set('offset',offset);params.set('limit',50);
  setView($('catalog-detail-title'),'textContent',()=>t("本地数据详情 · {p0}",{p0:tileId||t("全部产品")}));
  setView($('catalog-detail-meta'),'textContent',()=>t("正在按需读取该瓦片记录…"));
  setView($('catalog-detail-body'),'innerHTML',()=>'');
  $('catalog-prev').disabled=true;$('catalog-next').disabled=true;
  if(!$('catalog-detail-dialog').open)$('catalog-detail-dialog').showModal();
  try{
    const data=await api('/api/catalog?'+params);
    if(generation!==state.catalogGeneration||serial!==catalogDetailSerial)return;
    setView($('catalog-detail-meta'),'textContent',()=>t("共 {p0} 条，当前显示 {p1} 条 · {p2}",{p0:data.total,p1:data.returned,p2:data.catalog}));
    setView($('catalog-page'),'textContent',()=>`${data.returned?offset+1:0}–${offset+data.returned} / ${data.total}`);
    setView($('catalog-detail-body'),'innerHTML',()=>data.records.map(row=>`<tr><td>${escapeHtml(row.item_id)}</td><td>${escapeHtml(row.product_type)}</td><td>${escapeHtml(row.flight_direction||'—')} / ${escapeHtml(row.track??'—')}</td><td>${escapeHtml(row.start_datetime||row.datetime||row.month||'—')}<br>${escapeHtml(row.end_datetime||'')}</td><td><code>${escapeHtml(row.grid_id)}</code></td><td class="product-location">${escapeHtml(row.processing_level||t("未知"))} · ${escapeHtml(row.product_variant||t("未知"))}<br><code>${escapeHtml(row.processing_signature||t("未知"))}</code><br>${row.representation==='virtual'?t("虚拟视图"):row.representation==='physical'?t("实体产品"):t("未知")}<br>${escapeHtml(row.view_path||row.zarr_path||row.cog_path||'—')}<br>${escapeHtml(row.zarr_group||'')}</td><td>${escapeHtml(row.status)}</td><td><button type="button" class="secondary" data-product-id="${escapeHtml(row.item_id)}" ${row.status==='complete'?'':'disabled'}>${t("读取")}</button>${row.cog_path&&row.status==='complete'?`<button type="button" class="secondary" data-mosaic-id="${escapeHtml(row.item_id)}">${t("加入镶嵌")}</button>`:''}</td></tr>`).join(''));
    $('catalog-prev').disabled=offset===0;$('catalog-next').disabled=offset+data.returned>=data.total;
  }catch(error){if(generation===state.catalogGeneration&&serial===catalogDetailSerial)setView($('catalog-detail-meta'),'textContent',()=>error.message)}
}

async function selectCatalogProduct(itemId){
  const serial=++productReadSerial,generation=state.catalogGeneration;
  selectedProduct=null;$('product-reader').hidden=false;$('product-sample-form').hidden=true;
  $('derive-panel').hidden=true;$('view-panel').hidden=true;$('analysis-panel').hidden=true;
  setView($('product-identity'),'textContent',()=>itemId);setView($('product-warnings'),'textContent',()=>'');
  setView($('product-metadata'),'textContent',()=>'');setView($('product-sample'),'innerHTML',()=>'');
  setView($('product-message'),'textContent',()=>t("正在读取产品元数据…"));
  const params=new URLSearchParams({root_id:catalogDetailParams.get('root_id'),output:catalogDetailParams.get('output'),item_id:itemId});
  try{
    const data=await api('/api/catalog/product?'+params);
    if(serial!==productReadSerial||generation!==state.catalogGeneration)return;
    selectedProduct={params,data};
    setupViewForm(data,params);
    setupAnalysisForm(data);
    const canDerive=data.record.representation==='physical'&&['scenes','smonthly','monthly'].includes(data.record.product_type)&&data.variables.VV_dB&&data.variables.VH_dB;
    $('derive-panel').hidden=!canDerive;$('derive-panel').open=false;
    if(canDerive){
      $('derive-start').value=(data.time_range?.[0]||'').slice(0,19);$('derive-end').value=(data.time_range?.[1]||'').slice(0,19);
      $('derive-output').value='derived/cube-'+Date.now();$('derive-x').value=0;$('derive-y').value=0;
      $('derive-width').value=data.sizes.x;$('derive-height').value=data.sizes.y;
      $('derive-interpolation').value='';$('derive-domain').value='';$('derive-gap').value='';
      $('derive-filter').value='none';$('derive-monthly').value='';syncDeriveInterpolation();
      document.querySelectorAll('[name=derive-feature]').forEach(el=>el.checked=el.value!=='glcm');
      setView($('derive-message'),'textContent',()=>'');
    }
    setView($('product-warnings'),'textContent',()=>data.warnings.length?t("读取提示：{p0}",{p0:data.warnings.join('; ')}):'');
    setView($('product-metadata'),'textContent',()=>JSON.stringify(data,null,2));
    setView($('product-message'),'textContent',()=>t("已选择：{p0}",{p0:itemId}));
    const bands=Object.entries(data.variables).filter(([,v])=>v.dimensions.includes('x')&&v.dimensions.includes('y')&&v.dimensions.every(d=>['x','y','time'].includes(d))).map(([name])=>name);
    $('sample-band').replaceChildren(...bands.map(band=>new Option(band,band)));
    $('sample-row').value=0;$('sample-col').value=0;$('sample-row').max=data.sizes.y-1;$('sample-col').max=data.sizes.x-1;
    for(const id of ['sample-start','sample-end'])$(id).value='';
    $('sample-band').onchange=()=>{
      const temporal=data.variables[$('sample-band').value]?.dimensions.includes('time');
      for(const id of ['sample-start','sample-end'])$(id).disabled=!temporal;
    };
    $('sample-band').onchange();
    $('product-sample-form').hidden=!bands.length;$('read-product').disabled=false;
  }catch(error){if(serial===productReadSerial&&generation===state.catalogGeneration)setView($('product-message'),'textContent',()=>error.message)}
}

async function readProductSample(event){
  event?.preventDefault();if(!selectedProduct)return;
  const serial=++productReadSerial,generation=state.catalogGeneration,params=new URLSearchParams(selectedProduct.params);
  params.set('band',$('sample-band').value);params.set('row',$('sample-row').value);params.set('col',$('sample-col').value);
  for(const key of ['start','end'])if(!$('sample-'+key).disabled&&$('sample-'+key).value)params.set(key,$('sample-'+key).value);
  $('read-product').disabled=true;setView($('product-sample'),'innerHTML',()=>'');
  setView($('product-message'),'textContent',()=>t("正在读取所选像元…"));
  try{
    const data=await api('/api/catalog/sample?'+params);
    if(serial!==productReadSerial||generation!==state.catalogGeneration)return;
    const sample=data.sample;
    setView($('product-warnings'),'textContent',()=>data.warnings.length?t("读取提示：{p0}",{p0:data.warnings.join('; ')}):'');
    setView($('product-metadata'),'textContent',()=>JSON.stringify({...data,sample:undefined},null,2));
    setView($('product-message'),'textContent',()=>t("{p0} · 行 {p1}，列 {p2} · {p3} 个值",{p0:sample.band,p1:sample.row,p2:sample.col,p3:sample.values.length}));
    setView($('product-sample'),'innerHTML',()=>sample.values.map((value,index)=>`<tr><td>${escapeHtml(sample.times?.[index]||t("静态"))}</td><td>${value===null?'null':escapeHtml(value)}</td></tr>`).join(''));
  }catch(error){if(serial===productReadSerial&&generation===state.catalogGeneration)setView($('product-message'),'textContent',()=>error.message)}
  finally{if(serial===productReadSerial)$('read-product').disabled=false;}
}

function setupViewForm(data,params){
  const virtual=data.record.representation==='virtual',physical=data.record.representation==='physical'&&data.variables.VV_dB&&data.variables.VH_dB;
  $('view-panel').hidden=!(virtual||physical);$('view-panel').open=false;
  const context=JSON.stringify([params.get('root_id'),params.get('output')]);
  if($('view-parents').dataset.context!==context){$('view-parents').value='';$('view-parents').dataset.context=context;}
  for(const option of $('view-operation').options)option.disabled=option.value==='view_materialize'?!virtual:!physical;
  $('view-operation').value=virtual?'view_materialize':'view_create';
  $('view-output').value='derived/view-'+Date.now();
  $('view-start').value=(data.time_range?.[0]||'').slice(0,19);$('view-end').value=(data.time_range?.[1]||'').slice(0,19);
  $('view-start-month').value=(data.time_range?.[0]||'').slice(0,7);$('view-end-month').value=(data.time_range?.[1]||'').slice(0,7);
  $('view-x').value=0;$('view-y').value=0;$('view-width').value=data.sizes.x;$('view-height').value=data.sizes.y;
  $('view-method').value='first_valid_pixel';document.querySelectorAll('[name=view-band]').forEach(n=>n.checked=true);
  const available=Boolean(state.capabilities?.tools?.views?.available);
  $('plan-view').disabled=!available;
  setView($('view-message'),'textContent',()=>available?'':t("缺少视图依赖，请安装 s1grits[views]。"));syncViewOperation();
}

function syncViewOperation(){
  const op=$('view-operation').value;
  for(const [id,show] of [['view-selection',op==='view_create'],['view-multitrack',op==='view_multitrack'],['view-window',op!=='view_materialize']]){
    $(id).hidden=!show;$(id).querySelectorAll('input,select,textarea,button').forEach(n=>n.disabled=!show);
  }
}

async function preflightView(event){
  event.preventDefault();if(!selectedProduct)return;
  const parent=selectedProduct,operation=$('view-operation').value;
  const request={operation,root_id:parent.params.get('root_id'),output:parent.params.get('output'),output_subdir:$('view-output').value};
  if(operation==='view_multitrack')Object.assign(request,{item_ids:$('view-parents').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),start_month:$('view-start-month').value,end_month:$('view-end-month').value,method:$('view-method').value});
  else request.item_id=parent.data.record.item_id;
  if(operation==='view_create')Object.assign(request,{start:$('view-start').value+'Z',end:$('view-end').value+'Z',bands:[...document.querySelectorAll('[name=view-band]:checked')].map(n=>n.value)});
  if(operation==='view_materialize')request.chunk_size=256;
  else request.window=['x','y','width','height'].map(key=>Number($('view-'+key).value));
  $('plan-view').disabled=true;setView($('view-message'),'textContent',()=>t("正在预检派生任务…"));
  try{
    const plan=await api('/api/tools/plan',{method:'POST',body:JSON.stringify(request)});
    if(parent!==selectedProduct)return;
    state.plan=plan;setView($('confirm-title'),'textContent',()=>t("确认视图任务"));
    setView($('confirm-message'),'textContent',()=>'');setView($('plan-strategies'),'textContent',()=>'');
    setView($('plan-summary'),'innerHTML',()=>`<p>${plan.result_type==='virtual'?t("虚拟视图只保存定义，不写完整数组。父数据必须保持可访问。"):t("本次将写入新的实体 Cube。")}</p><p>${t("对应实体数据量：{p0} GiB",{p0:plan.materialized_gib.toFixed(6)})}</p><p><code>${escapeHtml(plan.result_item_id)}</code></p><pre>${escapeHtml(JSON.stringify(plan.request,null,2))}</pre><p>${escapeHtml(plan.warnings.join('; '))}</p><details><summary>CLI</summary><pre>${escapeHtml(JSON.stringify(plan.command))}</pre></details><p>${t("派生任务中断后不原地续写。保留已有输出，另选新目录重新提交。")}</p>`);
    $('phrase-wrap').hidden=!plan.confirmation_required;$('phrase').value='';
    setView($('phrase-example'),'textContent',()=>plan.confirmation_phrase);setView($('phrase-reason'),'textContent',()=>plan.confirmation_required?t('largeConfirmation'):'');
    updateConfirmation();$('confirm-dialog').showModal();setView($('view-message'),'textContent',()=>t("预检通过，请核对规划。"));
  }catch(error){setView($('view-message'),'textContent',()=>error.message)}
  finally{$('plan-view').disabled=false;}
}

function setupAnalysisForm(data){
  const supported=data.record.representation==='physical'&&data.time_range&&data.variables.VV_dB&&data.variables.VH_dB;
  $('analysis-panel').hidden=!supported;$('analysis-panel').open=false;
  if(!supported)return;
  $('analysis-start').value=data.time_range[0].slice(0,19);$('analysis-end').value=data.time_range[1].slice(0,19);
  $('analysis-output').value='analysis/landtrendr-'+Date.now();$('analysis-mode').value='pixel';
  for(const key of ['x','y'])$('analysis-'+key).value=0;
  for(const key of ['width','height'])$('analysis-'+key).value=1;
  for(const key of ['method','domain','direction','points'])$('analysis-'+key).value='';
  const available=Boolean(state.capabilities?.tools?.local?.landtrendr?.available);
  $('plan-analysis').disabled=!available;
  setView($('analysis-message'),'textContent',()=>available?'':t("需要单独安装 landtrendr==0.4.0（本地实验后端）。"));syncAnalysisMode();
}

function syncAnalysisMode(){
  const mode=$('analysis-mode').value;
  $('analysis-grid').hidden=mode==='points';$('analysis-point-list').hidden=mode!=='points';
  for(const key of ['x','y','width','height'])$('analysis-'+key).disabled=mode==='points'||(mode==='pixel'&&['width','height'].includes(key));
  $('analysis-points').disabled=mode!=='points';$('analysis-points').required=mode==='points';
}

async function preflightFileTool(event){
  event.preventDefault();const analysis=event.target.id==='analysis-form',prefix=analysis?'analysis':'mosaic';
  const parent=selectedProduct,context=catalogDetailParams,generation=state.catalogGeneration;
  if(analysis&&!parent)return;
  const params=analysis?parent.params:context;
  const request={root_id:params.get('root_id'),output:params.get('output'),output_subdir:$(prefix+'-output').value};
  $('plan-'+prefix).disabled=true;setView($(prefix+'-message'),'textContent',()=>t("正在预检派生任务…"));
  try{
    if(analysis){
      const mode=$('analysis-mode').value;
      Object.assign(request,{operation:mode==='pixel'?'analyze_segments':'analyze_segments_batch',item_id:parent.data.record.item_id,
        band:$('analysis-band').value,start:$('analysis-start').value+'Z',end:$('analysis-end').value+'Z',
        annual_method:$('analysis-method').value,annual_domain:$('analysis-domain').value,
        disturbance_direction:$('analysis-direction').value,min_annual_observations:Number($('analysis-minimum').value)});
      if(mode==='points')request.points=JSON.parse($('analysis-points').value);
      else request[mode]= (mode==='pixel'?['x','y']:['x','y','width','height']).map(k=>Number($('analysis-'+k).value));
    }else Object.assign(request,{operation:'mosaic_grid',item_ids:$('mosaic-parents').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),
      target_crs:$('mosaic-crs').value,resolution:Number($('mosaic-resolution').value),
      bounds:$('mosaic-bounds').value.trim().split(/[\s,]+/).map(Number),output_format:$('mosaic-format').value});
    const plan=await api('/api/tools/plan',{method:'POST',body:JSON.stringify(request)});
    if(generation!==state.catalogGeneration||(analysis&&parent!==selectedProduct)||context!==catalogDetailParams)return;
    state.plan=plan;setView($('confirm-title'),'textContent',()=>t("确认工具任务"));
    setView($('confirm-message'),'textContent',()=>'');setView($('plan-strategies'),'textContent',()=>'');
    setView($('plan-summary'),'innerHTML',()=>`<p>${t("已有产品：{p0}",{p0:escapeHtml(plan.parents.map(p=>p.record.item_id).join(', '))})}</p><p>${t("预计未压缩输出：{p0} GiB",{p0:plan.raw_gib.toFixed(6)})}</p><pre>${escapeHtml(JSON.stringify(plan.request,null,2))}</pre>${plan.samples?`<pre>${escapeHtml(JSON.stringify(plan.samples.map(({input,...sample})=>sample),null,2))}</pre>`:''}<p>${escapeHtml(plan.warnings.join('; '))}</p><details><summary>CLI</summary><pre>${escapeHtml(JSON.stringify(plan.command))}</pre></details>`);
    $('phrase-wrap').hidden=!plan.confirmation_required;$('phrase').value='';
    setView($('phrase-example'),'textContent',()=>plan.confirmation_phrase);setView($('phrase-reason'),'textContent',()=>plan.confirmation_required?t('largeConfirmation'):'');
    updateConfirmation();$('confirm-dialog').showModal();setView($(prefix+'-message'),'textContent',()=>t("预检通过，请核对规划。"));
  }catch(error){setView($(prefix+'-message'),'textContent',()=>error.message)}
  finally{$('plan-'+prefix).disabled=false;}
}

async function openToolResult(jobId){
  setView($('tool-result-body'),'textContent',()=>t("正在读取产品元数据…"));
  if(!$('tool-result-dialog').open)$('tool-result-dialog').showModal();
  try{
    const data=await api('/api/tools/results/'+encodeURIComponent(jobId));
    const pointTable=data.tables.point_summary;
    if(pointTable)pointTable.columns=['point_id','col','row','status','analysis_state','segments','valid_observations','model_observations','error'];
    setView($('tool-result-body'),'innerHTML',()=>`<p>${t("状态")}: ${escapeHtml(data.job_status)} / ${escapeHtml(data.receipt?.status||t("暂无结果回执"))} ${escapeHtml(data.receipt?.analysis_state||'')}</p><p><code>${escapeHtml(data.receipt?.output||data.output_dir)}</code></p>${data.result_type==='analysis'?`<p>${t("缺测、未拟合和失败均不表示零变化；逐点状态及原始回执如下。")}</p>`:`<p>${escapeHtml(data.receipt?.format||'')} ${escapeHtml(data.receipt?.contract?.shape?.join(' × ')||'')} ${escapeHtml(data.receipt?.contract?.resampling||'')}</p>`}${Object.entries(data.tables).map(([name,table])=>`<h3>${escapeHtml(name)} (${table.total})</h3><div class="table-wrap"><table><thead><tr>${table.columns.map(c=>`<th>${escapeHtml(c)}</th>`).join('')}</tr></thead><tbody>${table.rows.map(row=>`<tr>${table.columns.map(c=>`<td>${escapeHtml(row[c]??'null')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`).join('')}<details><summary>${t("原始回执")}</summary><pre>${escapeHtml(JSON.stringify(data.receipt,null,2))}</pre></details>`);
  }catch(error){setView($('tool-result-body'),'textContent',()=>error.message)}
}

function syncDeriveInterpolation(){
  const method=$('derive-interpolation').value;
  for(const node of [...document.querySelectorAll('[name=derive-feature]'),$('derive-filter'),$('derive-monthly')]){
    node.disabled=Boolean(method);
    if(method){if(node.type==='checkbox')node.checked=false;else node.value=node.id==='derive-filter'?'none':'';}
  }
  $('derive-domain').disabled=!method;$('derive-domain').required=Boolean(method);
  $('derive-gap').disabled=method!=='linear';$('derive-gap').required=method==='linear';
}

async function preflightDerive(event){
  event.preventDefault();if(!selectedProduct)return;
  const parent=selectedProduct,method=$('derive-interpolation').value;
  const request={operation:'derive_cube',root_id:parent.params.get('root_id'),output:parent.params.get('output'),item_id:parent.data.record.item_id,
    output_subdir:$('derive-output').value,start:$('derive-start').value+'Z',end:$('derive-end').value+'Z',
    features:[...document.querySelectorAll('[name=derive-feature]:checked')].map(el=>el.value),
    spatial_filter:$('derive-filter').value,monthly:$('derive-monthly').value||null,
    interpolation:method||null,interpolation_domain:method?$('derive-domain').value:null,
    max_gap_days:method==='linear'?Number($('derive-gap').value):null,
    window:['x','y','width','height'].map(key=>Number($('derive-'+key).value)),chunk_size:256};
  $('plan-derive').disabled=true;setView($('derive-message'),'textContent',()=>t("正在预检派生任务…"));
  try{
    const plan=await api('/api/tools/plan',{method:'POST',body:JSON.stringify(request)});
    if(parent!==selectedProduct)return;
    state.plan=plan;setView($('confirm-title'),'textContent',()=>t("确认派生规划"));
    setView($('plan-strategies'),'textContent',()=>'');
    setView($('confirm-message'),'textContent',()=>'');
    setView($('plan-summary'),'innerHTML',()=>`<p>${t("已有产品：{p0}",{p0:escapeHtml(request.item_id)})}</p><p>${t("预计未压缩输出：{p0} GiB",{p0:plan.raw_gib.toFixed(4)})}</p><pre>${escapeHtml(JSON.stringify(plan.request,null,2))}</pre><details><summary>CLI</summary><pre>${escapeHtml(JSON.stringify(plan.command))}</pre></details><p>${t("派生任务中断后不原地续写。保留已有输出，另选新目录重新提交。")}</p>`);
    $('phrase-wrap').hidden=!plan.confirmation_required;$('phrase').value='';
    setView($('phrase-example'),'textContent',()=>plan.confirmation_phrase);setView($('phrase-reason'),'textContent',()=>plan.confirmation_required?t('largeConfirmation'):'');
    updateConfirmation();$('confirm-dialog').showModal();setView($('derive-message'),'textContent',()=>t("预检通过，请核对规划。"));
  }catch(error){setView($('derive-message'),'textContent',()=>error.message)}
  finally{$('plan-derive').disabled=false;}
}

function renderCountList(title,values){const entries=Object.entries(values||{});return `<div class="report-list"><b>${escapeHtml(title)}</b>${entries.length?entries.map(([key,value])=>`<div>${escapeHtml(key)}：${value}</div>`).join(''):t("<div>无</div>")}</div>`}

async function generateCatalogReport(){
  if(!state.catalog?.valid)return;const generation=state.catalogGeneration,rootId=state.catalogRootId;setView($('catalog-message'),'textContent',()=>(t("正在生成覆盖与完整性报告…")));$('report-catalog').disabled=true;
  try{
    const data=await api('/api/catalog/report?'+new URLSearchParams({root_id:rootId,output:$('cat-output').value}));if(generation!==state.catalogGeneration||rootId!==state.catalogRootId)return;const overall=data.overall||{},gaps=data.gaps||{},range=overall.date_range||[null,null];
    setView($('report-summary'),'innerHTML',()=>([[t("记录"),overall.total_records||0],[t("瓦片"),overall.tile_count||0],[t("有效月份"),overall.total_months||0],[t("存在缺月"),gaps.tiles_with_gaps||0]].map(([label,value])=>`<div class="report-stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(t(label))}</span></div>`).join('')));
    setView($('report-counts'),'innerHTML',()=>(renderCountList(t("产品"),data.counts?.products)+renderCountList(t("状态"),data.counts?.statuses)+renderCountList(t("轨道"),data.counts?.directions)));
    setView($('report-gaps'),'innerHTML',()=>(t("<div class=\"report-list\"><div>时间范围：{p0} 至 {p1}</div><div>完整组合：{p2}</div><div>缺月组合：{p3}</div><div>报告瓦片行：{p4}{p5}</div></div>", {p0: escapeHtml(range[0]||'—'), p1: escapeHtml(range[1]||'—'), p2: gaps.tiles_complete||0, p3: gaps.tiles_with_gaps||0, p4: data.tile_rows_total||0, p5: data.truncated?t("（页面摘要已截断）"):''})));
    $('catalog-report').hidden=false;setView($('catalog-message'),'textContent',()=>(t("报告已生成 · {p0}", {p0: data.catalog.catalog})));
  }catch(error){if(generation===state.catalogGeneration)setView($('catalog-message'),'textContent',()=>(error.message))}
  finally{if(generation===state.catalogGeneration)$('report-catalog').disabled=!state.catalog?.valid}
}

async function bootstrap(){
  try{state.capabilities=await api('/api/capabilities');setView($('health'),'textContent',()=>(t("已连接 · S1-GRiTS {p0} · Schema v{p1} · {p2}", {p0: state.capabilities.version, p1: state.capabilities.catalog_schema_version, p2: state.capabilities.stac_format==='geoparquet'?'GeoParquet':state.capabilities.stac_format})));$('health').classList.add('ok')}catch(error){setView($('health'),'textContent',()=>(t("服务连接失败")));$('health').classList.add('error')}
  try{state.selectedTiles=new Set(JSON.parse(sessionStorage.getItem('s1grits_selected_tiles')||'[]').map(value=>String(value).toUpperCase()))}catch{state.selectedTiles=new Set()}
  await loadCatalogRoots();initMap();buildTags();setWorkflow('scenes');setSelection('tiles');setWorkspaceTab(location.hash==='#catalog'?'catalog':'tasks');syncTileSelection();await restoreCatalogSelection();await loadTasks();setInterval(loadTasks,3000);
}

document.querySelectorAll('[data-workflow]').forEach(el=>el.onclick=()=>setWorkflow(el.dataset.workflow));
document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>setSelection(el.dataset.mode));
$('apply-bbox').onclick=applyBBox;$('submit').onclick=preflight;$('confirm-run').onclick=confirmTask;$('refresh-tasks').onclick=loadTasks;
$('tiles').oninput=()=>{clearTimeout(tileInputTimer);tileInputTimer=setTimeout(()=>syncTileSelection({fromText:true}),160)};$('clear-tiles').onclick=clearTiles;$('select-candidates').onclick=()=>selectCandidates(true);$('deselect-candidates').onclick=()=>selectCandidates(false);$('clear-aoi').onclick=clearAOI;
$('include-static').onchange=()=>{$('static-options').hidden=!$('include-static').checked};
$('resolution').onchange=()=>{setView($('resampling-note'),'textContent',()=>($('resolution').value==='10'?t("10 米采用 NoData 感知双线性插值；后向散射先在功率域插值，再转换为 dB。分类静态层仍采用最近邻。"):t("30 米采用最近邻重投影，保持现有产品兼容。")))};
$('aoi-file').onchange=event=>uploadAOI([...event.target.files]);
$('phrase').oninput=updateConfirmation;$('copy-phrase').onclick=copyConfirmation;
$('browse-output').onclick=()=>openDirectoryBrowser($('output').value);
$('close-output').onclick=()=>$('output-dialog').close();$('dir-up').onclick=()=>browseDirectory($('dir-up').dataset.parent);$('create-folder').onclick=createFolder;
$('select-output').onclick=()=>{$('output').value=state.directory||'s1_cube';$('output-dialog').close()};
$('tasks-tab').onclick=()=>setWorkspaceTab('tasks');$('local-search-tab').onclick=async()=>{setWorkspaceTab('catalog');if(state.catalogRestorePending){const root=catalogRoot(state.catalogRootId),output=$('cat-output').value;await openCatalogSelection(root,output,{autoQuery:true,activateTab:false})}};
$('select-catalog-folder').onclick=openCatalogFolderDialog;$('change-catalog-folder').onclick=openCatalogFolderDialog;$('reselect-catalog-folder').onclick=openCatalogFolderDialog;$('open-manual-catalog').onclick=openManualCatalogDialog;$('catalog-folder-up').onclick=()=>loadCatalogFolders($('catalog-folder-up').dataset.path);$('retry-catalog-folders').onclick=()=>loadCatalogFolders($('retry-catalog-folders').dataset.path);$('select-current-catalog-folder').onclick=selectCurrentCatalogFolder;$('manual-catalog-folder').onclick=openManualCatalogFromBrowser;$('close-catalog-folder').onclick=()=>closeCatalogFolderDialog();$('cancel-catalog-folder').onclick=()=>closeCatalogFolderDialog();$('catalog-folder-dialog').onclick=event=>{if(event.target===$('catalog-folder-dialog'))closeCatalogFolderDialog()};$('catalog-folder-dialog').addEventListener('cancel',event=>{if(state.catalogFolderActionBusy)event.preventDefault();else catalogFolderRequestSerial++});$('catalog-folder-dialog').addEventListener('close',()=>{catalogFolderRequestSerial++;state.catalogFolderActionBusy=false});$('close-catalog-root').onclick=()=>$('catalog-root-dialog').close();$('register-catalog-root').onclick=registerCatalogRoot;$('forget-catalog-folder').onclick=forgetCatalogRoot;$('scan-catalog-children').onclick=scanCatalogChildren;$('catalog-root-dialog').onclick=event=>{if(event.target===$('catalog-root-dialog'))$('catalog-root-dialog').close()};
document.querySelectorAll('[data-basemap]').forEach(el=>el.onclick=()=>setBaseMap(el.dataset.basemap));$('query-catalog').onclick=()=>queryCatalog();$('report-catalog').onclick=generateCatalogReport;
$('clear-catalog-map').onclick=()=>{clearCatalogMap();setView($('catalog-message'),'textContent',()=>(t("本地数据地图图层已清除。")))};$('toggle-local-data').onclick=toggleCatalogCoverage;
$('close-catalog-detail').onclick=()=>$('catalog-detail-dialog').close();$('catalog-detail-dialog').onclick=event=>{if(event.target===$('catalog-detail-dialog'))$('catalog-detail-dialog').close()};
$('catalog-detail-dialog').addEventListener('close',()=>{catalogDetailSerial++;productReadSerial++;selectedProduct=null});
$('list-catalog').onclick=()=>openCatalogTileDetails(null);
$('catalog-prev').onclick=()=>openCatalogTileDetails(state.catalogSelectedTile,Math.max(0,catalogDetailOffset-50));
$('catalog-next').onclick=()=>openCatalogTileDetails(state.catalogSelectedTile,catalogDetailOffset+50);
$('catalog-detail-body').onclick=event=>{
  const button=event.target.closest('[data-product-id]');if(button&&!button.disabled)selectCatalogProduct(button.dataset.productId);
  const mosaic=event.target.closest('[data-mosaic-id]');
  if(mosaic){const ids=$('mosaic-parents').value.split(/\r?\n/).filter(Boolean);if(!ids.includes(mosaic.dataset.mosaicId))ids.push(mosaic.dataset.mosaicId);$('mosaic-parents').value=ids.join('\n');$('mosaic-panel').open=true;}
};
$('analysis-form').onsubmit=preflightFileTool;$('mosaic-form').onsubmit=preflightFileTool;$('analysis-mode').onchange=syncAnalysisMode;
$('product-sample-form').onsubmit=readProductSample;
$('derive-form').onsubmit=preflightDerive;$('derive-interpolation').onchange=syncDeriveInterpolation;
$('view-form').onsubmit=preflightView;$('view-operation').onchange=syncViewOperation;
$('view-add-parent').onclick=()=>{if(!selectedProduct)return;const ids=$('view-parents').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);const id=selectedProduct.data.record.item_id;if(!ids.includes(id))ids.push(id);$('view-parents').value=ids.join('\n');};
$('close-task-log').onclick=closeTaskLog;$('refresh-log').onclick=()=>pollTaskLog(false);$('task-log-dialog').onclick=event=>{if(event.target===$('task-log-dialog'))closeTaskLog()};
$('close-recovery').onclick=()=>{$('recovery-dialog').close();state.recovery=null};$('confirm-recovery').onclick=confirmRecovery;$('recovery-dialog').onclick=event=>{if(event.target===$('recovery-dialog')){$('recovery-dialog').close();state.recovery=null}};
$('toggle-mgrs').onclick=toggleMgrs;
$('map-error').onclick=()=>{$('map-error').hidden=true};
bootstrap();

document.addEventListener('s1-language-change',()=>{
  currentCatalogLayers.forEach(layer=>{if(layer.isPopupOpen())layer.getPopup().update()});
  if(map?.zoomControl){
    const zoom=map.zoomControl.getContainer();
    for(const [selector,key] of [['.leaflet-control-zoom-in','zoomIn'],['.leaflet-control-zoom-out','zoomOut']]){
      const button=zoom.querySelector(selector);button.title=t(key);button.setAttribute('aria-label',t(key));
    }
  }
});
