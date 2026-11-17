// Local-only browser contract fixture; all images and data are synthetic.
globalThis.canvasContract = async function(offscreen, suppliedCanvas) {
    const make = () => offscreen ? new OffscreenCanvas(32, 32) : Object.assign(document.createElement('canvas'), {width:32,height:32});
    const c=suppliedCanvas || make(), ctx=c.getContext('2d',{willReadFrequently:true});
    const expected=new Uint8ClampedArray(32*32*4);
    for(let y=0;y<32;y++)for(let x=0;x<32;x++){
        const i=(y*32+x)*4;
        expected.set([x*7%256,y*9%256,(x*13+y*11)%256,255],i);
    }
    ctx.putImageData(new ImageData(expected,32,32),0,0);
    const read=()=>Array.from(ctx.getImageData(0,0,32,32).data);
    const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
    const before=read(), readStable=same(before,read()), tile=[];
    for(let y=0;y<32;y++)for(let x=0;x<32;x++)tile.push(...ctx.getImageData(x,y,1,1).data);
    const negative=Array.from(ctx.getImageData(32,32,-32,-32).data);
    const blob=()=>offscreen?c.convertToBlob({type:'image/png'}):new Promise(resolve=>c.toBlob(resolve,'image/png'));
    const firstPending=blob(), secondPending=blob();
    const unchangedWhilePending=same(before,read());
    const first=await firstPending,second=await secondPending;
    const firstBytes=Array.from(new Uint8Array(await first.arrayBuffer()));
    const secondBytes=Array.from(new Uint8Array(await second.arrayBuffer()));
    const unchangedAfterExport=same(before,read());
    const pending=blob();
    ctx.fillStyle='rgb(190,80,30)';ctx.fillRect(0,0,8,8);
    const drawn=read();await pending;
    return {
        fullTileEqual:same(before,tile),negativeCropEqual:same(before,negative),
        readStable,
        noisy:!same(before,Array.from(expected)),
        unchangedWhilePending,unchangedAfterExport,drawPreserved:same(drawn,read()),
        repeatedExportsEqual:same(firstBytes,secondBytes),
        pixels:before,png:firstBytes,
        initialWorkerRead:typeof document==='undefined'?globalThis.fixtureFirstRead:null,
        marker:['__adStealth','__adWorkerStealth','__adWorkerBase'].some(k=>k in globalThis)
    };
};
