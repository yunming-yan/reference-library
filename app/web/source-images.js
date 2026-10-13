/* Shared original-image resolver. SVG data is parsed and sanitized before display. */
(()=>{
'use strict';
const SVG_NS='http://www.w3.org/2000/svg';
const blocked=new Set(['script','foreignobject','iframe','object','embed','audio','video','animate','animatemotion','animatetransform','set','discard']);
const cssProperties=new Set(('fill stroke stroke-width stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset fill-opacity stroke-opacity opacity fill-rule clip-rule paint-order vector-effect marker-start marker-mid marker-end stop-color stop-opacity flood-color flood-opacity lighting-color color background background-color font-family font-size font-weight font-style font-variant font-stretch letter-spacing word-spacing text-anchor dominant-baseline alignment-baseline white-space text-decoration shape-rendering text-rendering image-rendering display visibility clip-path mask filter transform transform-origin transform-box writing-mode unicode-bidi direction baseline-shift vertical-align').split(' '));
const cssDecode=value=>String(value).replace(/\\([0-9a-f]{1,6})\s?/gi,(_,hex)=>{const n=parseInt(hex,16);return n>0&&n<=0x10ffff?String.fromCodePoint(n):'\ufffd'}).replace(/\\([^\r\n])/g,'$1');
function safeCssValue(value){const decoded=cssDecode(value).replace(/\/\*[\s\S]*?\*\//g,'');if(/(?:@import|expression\s*\(|javascript\s*:|vbscript\s*:|behavior\s*:|-moz-binding)/i.test(decoded))return false;let valid=true;const rest=decoded.replace(/url\s*\(([^)]*)\)/gi,(_,inside)=>{const target=inside.trim().replace(/^(['"])([\s\S]*)\1$/,'$2');if(!/^#[^\s"'<>]*$/.test(target))valid=false;return ''});return valid&&!/url\s*\(/i.test(rest)}
function safeDeclarations(style){const result=[];for(const property of style){const value=style.getPropertyValue(property);if(cssProperties.has(property)&&safeCssValue(value))result.push(property+':'+value+(style.getPropertyPriority(property)==='important'?'!important':'')+';')}return result.join('')}
function safeStyleText(text){try{const sheet=new CSSStyleSheet();sheet.replaceSync(text);const rules=entries=>[...entries].map(rule=>{if(rule.selectorText&&rule.style){const declarations=safeDeclarations(rule.style);return declarations?rule.selectorText+'{'+declarations+'}':''}if(rule.cssRules&&/^(?:@media|@supports)\b/i.test(rule.cssText)){const inner=rules(rule.cssRules);return inner?rule.cssText.slice(0,rule.cssText.indexOf('{')+1)+inner+'}':''}return ''}).join('');return rules(sheet.cssRules)}catch{return ''}}
function sanitizeSvg(svg){
  if(!svg||svg.localName!=='svg'||svg.namespaceURI!==SVG_NS)return null;
  const copy=svg.cloneNode(true);
  copy.setAttribute('xmlns',SVG_NS);
  for(const el of [...copy.querySelectorAll('*')])if(el.namespaceURI!==SVG_NS||blocked.has(el.localName.toLowerCase()))el.remove();
  for(const el of [copy,...copy.querySelectorAll('*')]){
    for(const attribute of [...el.attributes]){
      const name=attribute.name.toLowerCase(),local=attribute.localName.toLowerCase(),value=attribute.value;
      if(/^on/i.test(local)||['src','srcset','xml:base','base','content','contenteditable','tabindex','autofocus'].includes(name)){el.removeAttributeNode(attribute);continue}
      if(local==='href'&&!/^#[^\s"'<>]*$/.test(value.trim())){el.removeAttributeNode(attribute);continue}
      if(name==='style'){const declarations=safeDeclarations(el.style);if(declarations)el.setAttribute('style',declarations);else el.removeAttribute('style');continue}
      if(/url\s*\(/i.test(cssDecode(value))&&!safeCssValue(value))el.removeAttributeNode(attribute);
    }
    if(el.localName.toLowerCase()==='style'){const text=safeStyleText(el.textContent);if(text)el.textContent=text;else el.remove()}
  }
  return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(copy));
}
function svgData(value){
  try{
    const source=String(value||'').trim(),comma=source.indexOf(',');if(comma<0)return null;
    const metadata=source.slice(0,comma),parts=metadata.split(';');if(parts.shift().toLowerCase()!=='data:image/svg+xml')return null;
    let base64=false;for(const part of parts){if(part.toLowerCase()==='base64'){if(base64)return null;base64=true}else if(!/^charset\s*=\s*utf-8$/i.test(part))return null}
    const payload=source.slice(comma+1);let text;
    if(base64){const encoded=payload.replace(/\s/g,'');if(!/^[a-z0-9+/]*={0,2}$/i.test(encoded))return null;const binary=atob(encoded);text=new TextDecoder('utf-8',{fatal:true}).decode(Uint8Array.from(binary,c=>c.charCodeAt(0)))}else text=decodeURIComponent(payload);
    if(/<!\s*(?:doctype|entity)\b/i.test(text))return null;
    const doc=new DOMParser().parseFromString(text,'image/svg+xml');if(doc.getElementsByTagName('parsererror').length)return null;
    return sanitizeSvg(doc.documentElement);
  }catch{return null}
}
function imageURL(value,base=''){
  if(typeof value!=='string'||!value.trim())return null;
  if(/^data:image\/svg\+xml(?:;|,)/i.test(value.trim()))return svgData(value);
  try{const url=new URL(value,location.origin+base);if(url.origin===location.origin&&['http:','https:'].includes(url.protocol)&&url.pathname.startsWith('/reports/'))return url.href;if(/^data:image\/(png|jpeg|gif|webp|avif|bmp);/i.test(value.trim()))return value.trim()}catch{}return null;
}
window.archiveSourceImages=Object.freeze({svgData,svgImage:sanitizeSvg,imageURL});
})();
