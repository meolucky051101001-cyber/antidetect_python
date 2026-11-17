// Bounded synthetic timings. No profile, network, or user data is sampled.
globalThis.canvasPerformance = async function({size, repetitions, offscreen}) {
    if(![32,256,1024].includes(size)||!Number.isInteger(repetitions)||repetitions<2||repetitions>10)throw new RangeError('Invalid bounded workload');
    const canvas=offscreen?new OffscreenCanvas(size,size):Object.assign(document.createElement('canvas'),{width:size,height:size});
    const ctx=canvas.getContext('2d',{willReadFrequently:true});
    const pixels=new Uint8ClampedArray(size*size*4);
    for(let y=0;y<size;y++)for(let x=0;x<size;x++)pixels.set([x*7%256,y*9%256,(x*13+y*11)%256,255],(y*size+x)*4);
    ctx.putImageData(new ImageData(pixels,size,size),0,0);
    const digest=async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(v=>v.toString(16).padStart(2,'0')).join('');
    const read=()=>ctx.getImageData(0,0,size,size).data;
    const hashBefore=await digest(read()), reads=[],exports=[],draws=[];
    let readStable=true;
    // One untimed warmup read prevents reporting an exclusively cold path.
    read();
    for(let i=0;i<repetitions;i++){
        let started=performance.now();const value=read();reads.push(performance.now()-started);
        readStable=readStable&&(await digest(value))===hashBefore;
        started=performance.now();
        const blob=offscreen?await canvas.convertToBlob({type:'image/png'}):await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
        if(!blob)throw new Error('PNG export failed');
        exports.push(performance.now()-started);
        // Same-size, separate destination does not modify the tested source.
        const target=offscreen?new OffscreenCanvas(size,size):Object.assign(document.createElement('canvas'),{width:size,height:size});
        started=performance.now();target.getContext('2d').drawImage(canvas,0,0);draws.push(performance.now()-started);
        target.width=target.height=0;
    }
    const hashAfter=await digest(read());
    const stats=values=>{const sorted=[...values].sort((a,b)=>a-b);return {samples_ms:values,min_ms:sorted[0],median_ms:sorted[Math.floor(sorted.length/2)],p95_ms:sorted[Math.ceil(sorted.length*.95)-1],max_ms:sorted[sorted.length-1]};};
    canvas.width=canvas.height=0;
    return {size,offscreen,repetitions,readStable,sourceUnchanged:hashBefore===hashAfter,
        hashBefore,hashAfter,read:stats(reads),png_export:stats(exports),draw:stats(draws)};
};
