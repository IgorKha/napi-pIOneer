"""Portable graphs + referenced Python blocks + shared libraries; atomic import rollback."""
import io
import json
import secrets
import shutil
import tempfile
import zipfile
from pathlib import Path
from pioneer.codetree import CodeTree, TreeError, TREE_LIMIT


def digest(tree):
    import hashlib
    return {f['path']:f['sha256'] for f in tree.files() if f['kind']=='file'}


def export_to(app,pid,target):
    pm,bm=app.state.projects,app.state.blocks
    meta,graph=pm._read(pid);flat,_,_=pm._flat(graph)
    required={pm.registry.get(n.type).key for n in flat.nodes.values()}
    body={'format':'pioneer-project-bundle','version':1,'name':meta.name,'description':meta.description,'graph':flat.to_json(),'blocks':[],'libraries':[],'modules':[]}
    sources=[]
    for block in bm.list():
        if {t.split('@')[0] for t in required}.intersection(block['types']):
            archive='blocks/'+block['id']+'.zip'
            sources.append((archive,CodeTree(app.state.cfg.data_dir/'blocks'/block['id'])))
            body['blocks'].append({'archive':archive,'types':block['types']})
    for module in app.state.modules.list():
        if required.intersection(module['types']):
            archive=f"modules/{module['id']}-{module['version']}.zip"
            sources.append((archive,CodeTree(Path(module['root']))))
            body['modules'].append({'archive':archive})
    for path in sorted((app.state.cfg.data_dir/'lib').glob('*/pioneer.json')):
        archive='libraries/'+path.parent.name+'.zip'
        sources.append((archive,CodeTree(path.parent)))
        body['libraries'].append({'id':path.parent.name,'archive':archive})
    # Bound all expanded sources and use disk for both archive levels: no 50 MB BytesIO copies in core.
    if len(sources)>127 or sum(f['size'] for _,tree in sources for f in tree.files() if f['kind']=='file')>TREE_LIMIT:
        raise TreeError('зависимости проекта больше 50 МиБ/127 пакетов')
    with tempfile.TemporaryDirectory(dir=app.state.cfg.data_dir) as tmp, zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for i,(archive,tree) in enumerate(sources):
            inner=Path(tmp)/(str(i)+'.zip');tree.export_to(inner);z.write(inner,archive);inner.unlink()
        z.writestr('project.json',json.dumps(body,ensure_ascii=False))
    if Path(target).stat().st_size>TREE_LIMIT:raise TreeError('пакет проекта больше 50 МиБ')


def export(app,pid):
    with tempfile.TemporaryDirectory(dir=app.state.cfg.data_dir) as tmp:
        target=Path(tmp)/'project.zip';export_to(app,pid,target);return target.read_bytes()


def import_bundle(app,data,confirmed):
    if not confirmed:raise TreeError('подтвердите импорт доверенного Python-кода: проверка схемы исполняет его в worker')
    bm,pm=app.state.blocks,app.state.projects
    created,libs,modules=[],[],[]
    with tempfile.TemporaryDirectory(dir=app.state.cfg.data_dir) as tmp:
        stage=Path(tmp);staged=[]; expanded=0; seen=set()
        try:
            with zipfile.ZipFile(data if isinstance(data,Path) else io.BytesIO(data)) as z:
                entries=z.infolist()
                if len(entries)>128 or sum(e.file_size for e in entries)>TREE_LIMIT or len({e.filename for e in entries})!=len(entries):raise TreeError('слишком большой/неоднозначный пакет')
                if z.getinfo('project.json').file_size>1024*1024:raise TreeError('граф больше 1 МиБ')
                body=json.loads(z.read('project.json'))
                if body['format']!='pioneer-project-bundle' or body['version']!=1:raise TreeError('неверный формат пакета')
                graph=body['graph']
                if not isinstance(graph,dict) or len(graph.get('nodes',[]))>500 or len(graph.get('edges',[]))>2000:raise TreeError('неверный граф')
                if len({n['id'] for n in graph.get('nodes',[])})!=len(graph.get('nodes',[])):raise TreeError('повторяющиеся id нод')
                if not isinstance(body['name'],str) or not isinstance(body.get('description',''),str):raise TreeError('неверные метаданные')
                for kind in ('blocks','libraries','modules'):
                    if not isinstance(body.get(kind,[]),list):raise TreeError('неверные зависимости')
                    if len(body.get(kind,[]))>64:raise TreeError('слишком много зависимостей')
                    for i,item in enumerate(body.get(kind,[])):
                        if not isinstance(item,dict):raise TreeError('неверное описание зависимости')
                        item.pop('reuse',None)
                        name=item['archive']
                        if name not in z.namelist() or not name.startswith(kind+'/') or not name.endswith('.zip'):raise TreeError('неверное вложение пакета')
                        if name in seen:raise TreeError('повторяющаяся зависимость')
                        seen.add(name)
                        raw=stage/(kind+str(i)+'.zip')
                        with z.open(name) as source,raw.open('xb') as target:shutil.copyfileobj(source,target,64*1024)
                        with zipfile.ZipFile(raw) as inner:
                            expanded+=sum(f.file_size for f in inner.infolist())
                        if expanded>TREE_LIMIT:raise TreeError('распакованные зависимости больше 50 МиБ')
                        target=stage/(kind+str(i));t=CodeTree(target);target.mkdir();t.import_zip(raw)
                        with zipfile.ZipFile(raw) as inner:
                            if inner.getinfo('pioneer.json').file_size>65536:raise TreeError('метаданные зависимости больше 64 КиБ')
                            meta=json.loads(inner.read('pioneer.json'))
                            if not isinstance(meta,dict):raise TreeError('неверные метаданные зависимости')
                            entry=meta.get('entry','block.py' if kind=='blocks' else 'main.py');t.path(entry)
                            t.set_meta(meta)
                        staged.append((kind,item,t))
            # Preflight libraries and type collisions before any writes/exec.
            existing=bm.list()
            for kind,item,t in staged:
                if kind=='libraries':
                    ident=item['id']
                    if not isinstance(ident,str) or not ident.isidentifier():raise TreeError('неверное имя библиотеки')
                    dest=app.state.cfg.data_dir/'lib'/ident
                    if dest.exists() and digest(CodeTree(dest))!=digest(t):raise TreeError('библиотека '+ident+' уже существует с другим кодом')
                elif kind=='blocks':
                    hits=[b for b in existing if set(b['types'])&set(item['types'])]
                    if hits:
                        if len(hits)!=1 or set(hits[0]['types'])!=set(item['types']) or digest(CodeTree(app.state.cfg.data_dir/'blocks'/hits[0]['id']))!=digest(t):raise TreeError('тип блока уже существует с другим кодом')
                        item['reuse']=True
            for kind,item,t in staged:
                if kind=='libraries':
                    dest=app.state.cfg.data_dir/'lib'/item['id']
                    if not dest.exists():
                        dest.parent.mkdir(exist_ok=True);shutil.copytree(t.root,dest);libs.append(dest)
                elif kind=='modules':
                    result=app.state.modules.install(t.export(),True)
                    if result['changed']:modules.append((result['id'],result['version']))
            for kind,item,t in staged:
                if kind!='blocks' or item.get('reuse'):continue
                ident=secrets.token_hex(4);dest=app.state.cfg.data_dir/'blocks'/ident
                shutil.copytree(t.root,dest);created.append(ident)
                if (dest/'requirements.txt').exists():
                    from pioneer.dependencies import install
                    install(dest,True)
                result=bm.save(ident,bm.get(ident)['code'])
                if result.get('error') or set(result['types'])!=set(item['types']):raise TreeError('неверная схема блока в пакете')
            return pm.create(body['name'],body.get('description',''),graph)
        except BaseException:
            for ident in created:
                bm.delete(ident)
            for path in libs:shutil.rmtree(path)
            for ident,version in modules:app.state.modules.remove(ident,version)
            raise
