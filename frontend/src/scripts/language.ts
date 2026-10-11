import * as monaco from 'monaco-editor/editor/editor.api.js';
export type Scope = {kind: string; id: string; path: string};
export function languageProviders(model:monaco.editor.ITextModel, scope:Scope) {
  async function query(action:string, position:monaco.Position, token:monaco.CancellationToken) {
    if(token.isCancellationRequested) return {items:[]};
    const abort=new AbortController();const cancel=token.onCancellationRequested(()=>abort.abort());
    try {
      const column=Array.from(model.getLineContent(position.lineNumber).slice(0,position.column-1)).length;
      const r=await fetch(`/pioneer/api/code/${scope.kind}/${scope.id}/language`,{method:'POST',signal:abort.signal,headers:{'X-Pioneer':'1','Content-Type':'application/json'},body:JSON.stringify({action,path:scope.path,code:model.getValue(),line:position.lineNumber,column})});
      return r.ok?await r.json():{items:[]};
    }catch{return {items:[]};}finally{cancel.dispose();}
  }
  const selector={language:'python',pattern:model.uri.path};
  const registrations=[
    monaco.languages.registerCompletionItemProvider(selector,{triggerCharacters:['.'],async provideCompletionItems(m,p,_ctx,t){
      if(m!==model)return {suggestions:[]};const r=await query('complete',p,t);const w=m.getWordUntilPosition(p);
      return {suggestions:r.items.map((i:{label:string;detail:string;type:string})=>({label:i.label,insertText:i.label,detail:i.detail,kind:i.type==='function'?monaco.languages.CompletionItemKind.Function:i.type==='class'?monaco.languages.CompletionItemKind.Class:monaco.languages.CompletionItemKind.Variable,range:new monaco.Range(p.lineNumber,w.startColumn,p.lineNumber,w.endColumn)}))};
    }}),
    monaco.languages.registerHoverProvider(selector,{async provideHover(m,p,t){if(m!==model)return null;const r=await query('hover',p,t);return r.items.length?{contents:r.items.map((i:{label:string;documentation:string})=>({value:`\`\`\`python\n${i.label}\n\`\`\`\n${i.documentation}`}))}:null;}}),
    monaco.languages.registerDefinitionProvider(selector,{async provideDefinition(m,p,t){if(m!==model)return [];const r=await query('goto',p,t);return r.items.map((i:{path:string;line:number;column:number})=>({uri:monaco.Uri.parse(`pioneer://code/${scope.kind}/${scope.id}/${i.path}`),range:new monaco.Range(i.line,i.column+1,i.line,i.column+1)}));}}),
    monaco.languages.registerSignatureHelpProvider(selector,{signatureHelpTriggerCharacters:['(',','],async provideSignatureHelp(m,p,t){if(m!==model)return null;const r=await query('signatures',p,t);if(!r.items.length)return null;return {value:{signatures:r.items.map((i:{label:string;parameters:string[]})=>({label:i.label,parameters:i.parameters.map(label=>({label}))})),activeSignature:0,activeParameter:r.items[0].parameter},dispose(){}};}}),
  ];
  let timer:ReturnType<typeof setTimeout>|undefined;
  let token:monaco.CancellationTokenSource|undefined;
  function diagnostics() {
    clearTimeout(timer);token?.cancel();token?.dispose();
    timer=setTimeout(async()=>{
      token=new monaco.CancellationTokenSource();const current=token;
      const version=model.getVersionId();const r=await query('diagnostics',new monaco.Position(1,1),current.token);
      if(current.token.isCancellationRequested||model.isDisposed()||model.getVersionId()!==version)return;
      monaco.editor.setModelMarkers(model,'pioneer-python',r.items.map((i:{line:number;column:number;message:string})=>({severity:monaco.MarkerSeverity.Error,message:i.message,startLineNumber:i.line,endLineNumber:i.line,startColumn:i.column+1,endColumn:i.column+2})));
    },1200);
  }
  const changed=model.onDidChangeContent(diagnostics);diagnostics();
  return ()=>{clearTimeout(timer);token?.cancel();token?.dispose();changed.dispose();registrations.forEach(r=>r.dispose());};
}
