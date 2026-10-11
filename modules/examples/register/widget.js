export function mount(el,props,emit){
  const label=document.createElement('p');label.textContent=`Регистр: ${props.state.value}`;
  const input=document.createElement('input');input.type='number';input.min='0';input.max='65535';input.value=props.state.value;
  const button=document.createElement('button');button.textContent='Изменить на стенде';button.onclick=()=>emit('set',{value:Number(input.value)});
  el.append(label,input,button);
}
