import assert from 'node:assert/strict';
import {parseMarkdown,guideLink} from '../src/help/content.ts';
import {graphPreview,edgeKey} from '../src/projects/preview.ts';
assert.deepEqual(parseMarkdown('intro\n> quote\n\n| a | b |\n| --- | --- |\n| 1 | x\\|y |').map(b=>b.kind), ['p','quote','table']);
assert.equal(parseMarkdown('| a | b |\n| --- | --- |\n| 1 | x\\|y |')[0].rows[0][1], 'x|y');
assert.equal(parseMarkdown('```python run\nprint(1)\n```')[0].run,true);
assert.equal(parseMarkdown('```python\n# visible\n```\n<!-- hidden -->')[0].code,'# visible');
assert.equal(parseMarkdown('| ordinary text')[0].text,'| ordinary text');
assert.equal(guideLink('blocks.md#%D0%BA%D0%BE%D0%B4'),'#/help/blocks/%D0%BA%D0%BE%D0%B4');
assert.equal(guideLink('ai.md#bad%'),'#/help/ai/bad%25');
assert.equal(guideLink('#тест','ai'),'#/help/ai/%D1%82%D0%B5%D1%81%D1%82');
assert.equal(guideLink('https://example.com/docs.md'),'https://example.com/docs.md');
const n=(id,params={})=>({id,type:'builtin.watch@1',position:[0,0],params,inputs:{}});
const e={from:['old','value'],to:['keep','value']};
const before={nodes:[n('keep',{a:1,b:2}),n('old'),n('edit')],edges:[e],groups:[]};
const after={nodes:[n('keep',{b:2,a:1}),n('edit',{a:3}),n('new')],edges:[],groups:[]};
const preview=graphPreview(before,after);
assert.equal(preview.nodes.has('keep'),false);
assert.equal(preview.nodes.get('edit'),'changed');
assert.equal(preview.nodes.get('new'),'added');
assert.equal(preview.nodes.get('old'),'removed');
assert.equal(preview.edges.get(edgeKey(e)),'removed');
assert.equal(preview.graph.nodes.length,4);
assert.equal(after.nodes.length,3);
assert.equal(before.edges.length,1);
console.log('Markdown, guide links, and AI graph diff: passed');

// review #19: an old connection cannot overwrite the replacement snapshot.
const {loadSnapshot} = await import('../src/liveSnapshot.ts');
let resolveOld, resolveNew;
const received=[];
const cancel=loadSnapshot(()=>new Promise(r=>{resolveOld=r}),v=>received.push(v),e=>{throw e});
cancel();
loadSnapshot(()=>new Promise(r=>{resolveNew=r}),v=>received.push(v),e=>{throw e});
resolveNew({values:{fresh:2},nodes:{}}); await Promise.resolve();
resolveOld({values:{stale:1},nodes:{error:'old'}}); await Promise.resolve();
assert.deepEqual(received,[{values:{fresh:2},nodes:{}}]);
console.log('review #19 reconnect snapshots: passed');

// review #20: overlapping graph mutations use the preceding response revision.
const {SerialQueue}=await import('../src/serialQueue.ts');
const queue=new SerialQueue(); let rev=7, release;
const calls=[];
const first=queue.run(async()=>{calls.push(rev);await new Promise(r=>{release=r});rev=8;});
const second=queue.run(async()=>{calls.push(rev);rev=9;});
await Promise.resolve(); assert.deepEqual(calls,[7]);
release(); await Promise.all([first,second]); assert.deepEqual(calls,[7,8]);
await assert.rejects(queue.run(async()=>{throw new Error('failed');}));
await queue.run(async()=>{calls.push(rev);}); assert.equal(calls.at(-1),9);
console.log('review #20 serialized mutations: passed');

// review #21: Run awaits newer edits, including edits during tree refresh.
const {saveDrafts}=await import('../src/scripts/saveDrafts.ts');
let drafts={'main.py':{text:'old',saved:'',binary:false}};
let finishWrite, refreshCount=0; const writes=[];
const save=saveDrafts(()=>drafts,async(path,text)=>{
  writes.push(text); if(text==='old')await new Promise(r=>{finishWrite=r});
},(path,text)=>{drafts[path]={...drafts[path],saved:text};},async()=>{
  if(!refreshCount++)drafts['main.py'].text='newest';
});
drafts['main.py'].text='new'; finishWrite(); await save;
assert.deepEqual(writes,['old','newest']);
assert.equal(drafts['main.py'].saved,'newest');
await assert.rejects(saveDrafts(()=>({'main.py':{text:'bad',saved:'',binary:false}}),async()=>{throw new Error('disk full');},()=>{},async()=>{}));
console.log('review #21 save-before-run: passed');

// Wire insertion respects both endpoints and keeps ambiguous socket pairs explicit.
const {insertionOptions,insertInEdge}=await import('../src/projects/sockets.ts');
const socket=(name,type)=>({name,type,title:name,doc:''});
const schema=(type,inputs,outputs)=>({type,title:type,inputs,outputs});
const choices=insertionOptions([
  schema('math',[socket('a','Number'),socket('b','Number')],[socket('value','Number')]),
  schema('text',[socket('value','String')],[socket('value','String')]),
  schema('event',[socket('trigger','Event')],[socket('done','Event')]),
  schema('source',[],[socket('value','Number')]),
  schema('sink',[socket('value','Number')],[]),
], 'Number','Number');
assert.deepEqual(choices.map(o=>[o.schema.type,o.input.name,o.output.name]),[['math','a','value'],['math','b','value']]);
assert.equal(insertionOptions([schema('event',[socket('trigger','Event')],[socket('done','Event')])],'Event','Event').length,1);
assert.equal(insertionOptions([schema('any',[socket('in','Any')],[socket('out','Any')])],'Number','Number').length,0);
assert.deepEqual(insertInEdge(e,'inserted','math','a','value',[100,200]),[
  {op:'disconnect',to:['keep','value']},
  {op:'add_node',id:'inserted',type:'math',position:[100,200]},
  {op:'connect',from:['old','value'],to:['inserted','a']},
  {op:'connect',from:['inserted','value'],to:['keep','value']},
]);
assert.deepEqual(e,{from:['old','value'],to:['keep','value']});
console.log('Wire insertion socket compatibility and atomic operations: passed');
