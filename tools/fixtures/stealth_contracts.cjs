/* Synthetic Web API fixture. No browser, network, or user profile is used. */
const fs = require('fs');
const vm = require('vm');
const pageScript = fs.readFileSync(process.argv[2], 'utf8');
const workerScript = fs.readFileSync(process.argv[3], 'utf8');
const setup = `
const pending = [];
class ImageData {
    constructor(data, width, height) { this.data=data; this.width=width; this.height=height; }
}
class CanvasContext {
    constructor(canvas) { this.canvas=canvas; }
    getImageData(sx,sy,sw,sh) {
        if (!(this instanceof CanvasContext)) throw new TypeError('Illegal invocation');
        if (this.canvas.tainted) throw new Error('SecurityError');
        sx=Number(sx)|0;sy=Number(sy)|0;sw=Number(sw)|0;sh=Number(sh)|0;
        if(!sw||!sh)throw new Error('IndexSizeError');
        if(sw<0){sx+=sw;sw=-sw;}if(sh<0){sy+=sh;sh=-sh;}
        const data=new Uint8ClampedArray(sw*sh*4);
        for(let y=0;y<sh;y++)for(let x=0;x<sw;x++){
            if(sx+x>=0&&sy+y>=0&&sx+x<this.canvas.width&&sy+y<this.canvas.height){
                const start=((sy+y)*this.canvas.width+sx+x)*4;
                data.set(this.canvas.pixels.subarray(start,start+4),(y*sw+x)*4);
            }
        }
        return new ImageData(data,sw,sh);
    }
    createImageData(w,h){return new ImageData(new Uint8ClampedArray(w*h*4),w,h);}
    putImageData(img,dx,dy){
        for(let y=0;y<img.height;y++)for(let x=0;x<img.width;x++){
            if(dx+x>=0&&dy+y>=0&&dx+x<this.canvas.width&&dy+y<this.canvas.height)
                this.canvas.pixels.set(img.data.subarray((y*img.width+x)*4,(y*img.width+x+1)*4),((dy+y)*this.canvas.width+dx+x)*4);
        }
    }
    getContextAttributes(){return {colorSpace:'srgb',alpha:true};}
    measureText(){return {width:20};}
}
class GL{
    constructor(canvas){this.canvas=canvas;this.RGBA=6408;this.UNSIGNED_BYTE=5121;}
    getParameter(param){return param===37445?'native-vendor':'native-renderer';}
    getSupportedExtensions(){return ['EXT_native'];}
    getExtension(name){return name==='EXT_native'?{}:null;}
    getShaderPrecisionFormat(){return {precision:23};}
    readPixels(x,y,w,h,format,type,array,offset=0){
        if(!(this instanceof GL))throw new TypeError('Illegal invocation');
        if(w<0||h<0||format!==6408||type!==5121)return;
        for(let i=0;i<w*h*4&&offset+i<array.length;i++)array[offset+i]=77;
    }
}
class Canvas{
    constructor(w=16,h=16){this._width=w;this._height=h;this.pixels=new Uint8ClampedArray(w*h*4);this.context=null;}
    get width(){return this._width;}
    set width(v){this._width=v;this.pixels=new Uint8ClampedArray(this._width*this._height*4);}
    get height(){return this._height;}
    set height(v){this._height=v;this.pixels=new Uint8ClampedArray(this._width*this._height*4);}
    getContext(kind){
        if(this.context)return this.kind===kind?this.context:null;
        if(kind==='2d')this.context=new CanvasContext(this);
        else if(kind==='webgl')this.context=new GL(this);
        else return null;
        this.kind=kind;return this.context;
    }
    toDataURL(){
        if(!(this instanceof Canvas))throw new TypeError('Illegal invocation');
        if(this.tainted)throw new Error('SecurityError');
        return JSON.stringify(Array.from(this.pixels));
    }
    toBlob(cb){
        if(!(this instanceof Canvas))throw new TypeError('Illegal invocation');
        if(typeof cb!=='function')throw new TypeError('callback required');
        if(this.tainted)throw new Error('SecurityError');
        const pixels=Array.from(this.pixels);pending.push(()=>cb(pixels));
    }
}
class Offscreen extends Canvas{
    convertToBlob(){
        if(!(this instanceof Offscreen))throw new TypeError('Illegal invocation');
        if(this.tainted)return Promise.reject(new Error('SecurityError'));
        return Promise.resolve(Array.from(this.pixels));
    }
}
class DOMRect{constructor(x=1,y=2,width=3,height=4){Object.assign(this,{x,y,width,height});}}
class DOMRectList{constructor(){this[0]=new DOMRect();this.length=1;}item(i){return this[i]||null;}*[Symbol.iterator](){yield this[0];}}
class Element{getBoundingClientRect(){return new DOMRect();}getClientRects(){return new DOMRectList();}}
class MediaDeviceInfo{constructor(){this.kind='audioinput';this.label='';this.deviceId='';this.groupId='';}toJSON(){return {kind:this.kind};}}
class GPUAdapterInfo{}
class GPUAdapter{constructor(){this.info=new GPUAdapterInfo();}requestDevice(){if(!(this instanceof GPUAdapter))throw new TypeError();return Promise.resolve({});}}
class AudioBuffer{constructor(){this.data=new Float32Array([0.1,0.2,0.3]);}getChannelData(){return this.data;}copyFromChannel(dest){dest.set(this.data.subarray(0,dest.length));}}
const nativeDevices=[new MediaDeviceInfo()];
const nativeAdapter=new GPUAdapter();
const navigator={mediaDevices:{enumerateDevices(){return Promise.resolve(nativeDevices);}},gpu:{requestAdapter(){return Promise.resolve(nativeAdapter);}}};
const document={createElement(){return new Canvas();},fonts:{check(){return false;}}};
const native={getImageData:CanvasContext.prototype.getImageData,toBlob:Canvas.prototype.toBlob,convertToBlob:Offscreen.prototype.convertToBlob,rects:Element.prototype.getClientRects,devices:navigator.mediaDevices.enumerateDevices,adapter:navigator.gpu.requestAdapter,audio:AudioBuffer.prototype.getChannelData,readPixels:GL.prototype.readPixels};
Object.assign(globalThis,{ImageData,CanvasRenderingContext2D:CanvasContext,OffscreenCanvasRenderingContext2D:CanvasContext,HTMLCanvasElement:Canvas,OffscreenCanvas:Offscreen,WebGLRenderingContext:GL,DOMRect,DOMRectList,Element,MediaDeviceInfo,GPUAdapterInfo,GPUAdapter,AudioBuffer,navigator,document,window:globalThis,self:globalThis});
function fill(canvas){for(let i=0;i<canvas.pixels.length;i+=4){canvas.pixels[i]=(i/4)%256;canvas.pixels[i+1]=(i*7)%256;canvas.pixels[i+2]=(i*13)%256;canvas.pixels[i+3]=255;}}
function same(a,b){return JSON.stringify(Array.from(a))===JSON.stringify(Array.from(b));}
function makeCanvas(off){const c=off?new Offscreen(16,16):new Canvas(16,16);fill(c);return c;}
`;
async function probe(payload,worker){
    const context=vm.createContext({});
    vm.runInContext(setup,context);vm.runInContext(payload,context);
    return await vm.runInContext(`(async()=>{
        const c=makeCanvas(${worker});const ctx=c.getContext('2d');
        const full=ctx.getImageData(0,0,16,16).data;
        const tiles=new Uint8ClampedArray(full.length);
        for(let y=0;y<16;y++)for(let x=0;x<16;x++)tiles.set(ctx.getImageData(x,y,1,1).data,(y*16+x)*4);
        const neg=ctx.getImageData(16,16,-16,-16).data;
        const source=Array.from(c.pixels);
        let first,second,drawPreserved=true,temporarilyChanged=false;
        if(${worker}){
            const pendingFirst=c.convertToBlob();temporarilyChanged=!same(source,c.pixels);first=await pendingFirst;second=await c.convertToBlob();
        }else{
            c.toBlob(value=>{first=value;});temporarilyChanged=!same(source,c.pixels);
            c.pixels[0]=199;while(pending.length)pending.shift()();drawPreserved=c.pixels[0]===199;
            c.pixels.set(source);c.toBlob(value=>{second=value;});while(pending.length)pending.shift()();
        }
        const glCanvas=makeCanvas(false);const gl=glCanvas.getContext('webgl');
        const pixels=new Uint8Array(20).fill(9);gl.readPixels(0,0,1,1,6408,5121,pixels,4);
        const invalid=new Uint8Array(16).fill(9);gl.readPixels(0,0,-1,1,6408,5121,invalid);
        const untouched=new Canvas();untouched.toDataURL();
        const tainted=makeCanvas(${worker});tainted.getContext('2d');tainted.tainted=true;
        let taintedError=false;try{if(${worker})await tainted.convertToBlob();else tainted.toDataURL();}catch(e){taintedError=true;}
        let conversions=0;
        const objectOrigin={valueOf(){conversions++;return 0;}};
        ctx.getImageData(objectOrigin,0,1,1);
        let zeroSizeError=false;try{ctx.getImageData(0,0,0,1);}catch(e){zeroSizeError=true;}
        let illegalReceiver=false;try{CanvasContext.prototype.getImageData.call({},0,0,1,1);}catch(e){illegalReceiver=true;}
        const alpha=makeCanvas(${worker});const alphaContext=alpha.getContext('2d');
        for(let i=3;i<alpha.pixels.length;i+=4)alpha.pixels[i]=128;
        const alphaUnchanged=same(alphaContext.getImageData(0,0,16,16).data,alpha.pixels);
        const spill=ctx.getImageData(-1,-1,1,1).data;
        const outsideUnchanged=Array.from(spill).every(v=>v===0);
        const rects=new Element().getClientRects();const devices=await navigator.mediaDevices.enumerateDevices();
        const adapter=await navigator.gpu.requestAdapter();const audio=new AudioBuffer();const originalAudio=Array.from(audio.data);audio.getChannelData(0);
        return {
            canvasPixels:Array.from(full),
            conversions,zeroSizeError,illegalReceiver,alphaUnchanged,outsideUnchanged,
            marker:('__adStealth' in globalThis)||('__adWorkerStealth' in globalThis)||('__adWorkerBase' in globalThis),
            fullTileEqual:same(full,tiles),negativeCropEqual:same(full,neg),noisy:!same(full,source),
            sourcePreserved:same(source,c.pixels),repeatedExportsEqual:same(first,second),drawPreserved,temporarilyChanged,
            pixels:Array.from(pixels),invalidUnchanged:Array.from(invalid).every(v=>v===9),
            untouchedContext:untouched.context===null,taintedError,
            rectsNative:rects instanceof DOMRectList&&!Array.isArray(rects),
            mediaNative:devices===nativeDevices&&devices[0] instanceof MediaDeviceInfo,
            adapterNative:adapter===nativeAdapter&&adapter.info instanceof GPUAdapterInfo,
            audioNative:same(audio.data,originalAudio)
        };
    })()`,context);
}
(async()=>{process.stdout.write(JSON.stringify({page:await probe(pageScript,false),worker:await probe(workerScript,true)}));})().catch(e=>{console.error(e);process.exit(1);});
