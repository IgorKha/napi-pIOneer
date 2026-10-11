import {useEffect,useRef,useState} from 'react';
import {Markdown} from './help/Markdown';
import {api,type BenchModel} from './api';
type Module={id:string;version:number;title:string;source:string;commit:string|null;connection:string;types:string[];model_kind:string|null;model:{params:Record<string,{default?:unknown}>;actions:string[]}|null};
async function request(path='',body?:unknown,file?:File,method?:string){
  const r=await fetch('/pioneer/api/modules'+path,{method:method??(body||file?'POST':'GET'),headers:{'X-Pioneer':'1',...(file?{}:{'Content-Type':'application/json'})},body:file??(body?JSON.stringify(body):undefined)});
  const data=await r.json();if(!r.ok)throw new Error(data.detail??'Модули недоступны');return data;
}
export function ModuleWidget({model,onError}:{model:BenchModel&{widget?:string};onError:(e:string)=>void}){
  const frame=useRef<HTMLIFrameElement>(null);const lastState=useRef('');const [source,setSource]=useState('');
  useEffect(()=>{let active=true;setSource('');if(model.widget)fetch(model.widget).then(r=>{if(!r.ok)throw new Error('Виджет недоступен');return r.text();}).then(js=>{if(active)setSource(js);},e=>onError(e.message));return()=>{active=false;};},[model.widget]);
  useEffect(()=>{
    const listener=(event:MessageEvent)=>{if(event.source!==frame.current?.contentWindow)return;if(event.data?.type==='module-ready'){lastState.current='';state();return;}if(event.data?.type!=='module-action')return;const {name,payload}=event.data;if(!model.actions.includes(name)||JSON.stringify(payload??{}).length>8192)return;api.benchAction(model.id,name,payload??{}).catch(e=>onError(e.message));};
    window.addEventListener('message',listener);return()=>window.removeEventListener('message',listener);
  },[model.id,model.actions,model.state,onError]);
  const state=()=>{const props={state:model.state,config:model.config};const serialized=JSON.stringify(props);if(lastState.current===serialized)return;lastState.current=serialized;frame.current?.contentWindow?.postMessage({type:'module-state',props},'*');};
  useEffect(state,[model.state]);
  if(!source)return <pre className="text-xs">{JSON.stringify(model.state,null,2)}</pre>;
  const doc=`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'"><style>body{color:#ddd;background:#1d1d1d;font:13px system-ui}</style><div id="root"></div><script type="module">${source.replace(/<\/script/gi,'<\\/script')}\nwindow.addEventListener('message',e=>{if(e.source!==parent||e.data?.type!=='module-state')return;document.getElementById('root').replaceChildren();mount(document.getElementById('root'),e.data.props,(name,payload)=>parent.postMessage({type:'module-action',name,payload},'*'));});parent.postMessage({type:'module-ready'},'*');<\/script>`;
  return <iframe ref={frame} sandbox="allow-scripts" title={`Симулятор ${model.title}`} srcDoc={doc} onLoad={()=>{lastState.current="";state();}} className="mt-2 h-44 w-full rounded border border-line"/>;
}
export function ModulesPage(){
  const [items,setItems]=useState<Module[]>([]),[bench,setBench]=useState(false),[models,setModels]=useState<BenchModel[]>([]);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[trusted,setTrusted]=useState(false),[url,setUrl]=useState(''),[ref,setRef]=useState('');
  const [readme,setReadme]=useState(''),[config,setConfig]=useState<Record<string,string>>({});
  async function load(){const r=await request();setItems(r.modules);setBench(r.bench);if(r.bench)setModels((await api.bench()).models);}
  useEffect(()=>{load().catch(e=>setError(e.message));const timer=window.setInterval(()=>api.bench().then(r=>setModels(r.models),()=>{}),1500);return()=>clearInterval(timer);},[]);
  async function run(fn:()=>Promise<unknown>){setError('');setBusy(true);try{await fn();window.dispatchEvent(new Event('pioneer-reference-changed'));await load();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section>
    <h2 className="mb-2 text-lg font-semibold">Модули периферии</h2>
    <p className="mb-4 text-xs text-muted">ZIP с module.yaml, Python-драйвером, нодами, README и необязательными симулятором/JS-виджетом. Версии установлены рядом; существующие графы сохраняют свой тип @version. Python исполняется в worker, виджет — в изолированном iframe.</p>
    <a href="/pioneer/api/modules/example.zip" download className="mb-4 block text-xs text-accent">Скачать учебный модуль: драйвер + симулятор + виджет</a>
    <label className="mb-3 flex gap-2 text-xs"><input type="checkbox" checked={trusted} onChange={e=>setTrusted(e.target.checked)}/>Доверяю коду модуля и его зависимостям</label>
    <label className={`block mb-4 text-accent ${!trusted||busy?'opacity-40':''}`}>Загрузить ZIP<input className="hidden" disabled={!trusted||busy} type="file" accept=".zip" onChange={e=>{const f=e.target.files?.[0];e.target.value='';if(f)run(()=>{if(f.size>50*1024*1024)throw new Error('ZIP больше 50 МиБ');return request('?confirmed=true',undefined,f);});}}/></label>
    <form className="mb-5 flex flex-wrap gap-2" onSubmit={e=>{e.preventDefault();run(()=>request('/git',{url,ref,confirmed:trusted}));}}><input aria-label="Git URL модуля" placeholder="https://…/module.git" value={url} onChange={e=>setUrl(e.target.value)} className="min-w-0 flex-1 rounded border border-line bg-bg p-2 text-xs"/><input aria-label="Тег или ветка модуля" placeholder="тег / ветка" value={ref} onChange={e=>setRef(e.target.value)} className="w-28 rounded border border-line bg-bg p-2 text-xs"/><button disabled={!trusted||busy||!url||!ref} className="text-xs text-accent disabled:opacity-40">Установить из git</button></form>
    {error&&<p role="alert" className="mb-4 text-xs text-err">{error}</p>}
    {items.map(m=>{const key=`${m.id}@${m.version}`;const value=config[key]??JSON.stringify(Object.fromEntries(Object.entries(m.model?.params??{}).map(([k,p])=>[k,p.default??null])),null,2);return <article key={key} className="mb-4 rounded border border-line p-4 text-xs"><h3 className="font-semibold">{m.title} · {key}</h3><p className="my-2 break-all text-muted">{m.source}{m.commit?` · ${m.commit}`:''}</p><p>{m.connection}</p><p className="my-2 font-mono text-muted">{m.types.join(', ')}</p><button className="text-accent" onClick={()=>request(`/${m.id}/${m.version}/readme`).then(r=>setReadme(r.markdown),e=>setError(e.message))}>Подключение и README</button><button className="ml-4 text-muted" onClick={()=>confirm(`Удалить модуль ${key}?`)&&run(()=>request(`/${m.id}/${m.version}`,undefined,undefined,'DELETE'))}>Удалить</button>{m.model_kind&&bench&&<div className="mt-3"><textarea aria-label={`Конфигурация симулятора ${key}`} value={value} onChange={e=>setConfig(c=>({...c,[key]:e.target.value}))} rows={3} className="w-full rounded border border-line bg-bg p-2 font-mono"/><button disabled={busy} className="mt-2 text-accent" onClick={()=>run(()=>api.benchAdd(m.model_kind!,JSON.parse(value)))}>Добавить на стенд</button></div>}</article>;})}
    {models.filter(m=>m.kind.startsWith('module.')).map(m=><article key={m.id} className="mb-4 rounded border border-line p-3 text-xs"><h3>{m.title}</h3><ModuleWidget model={m} onError={setError}/><button className="mt-2 text-muted" onClick={()=>run(()=>api.benchRemove(m.id))}>Убрать симулятор</button></article>)}
    {readme&&<div className="mt-5 border-t border-line pt-4"><Markdown source={readme}/></div>}
  </section>;
}
