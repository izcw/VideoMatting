// 从 Client 目录使用 pnpm exec electron ../tools/py-VideoMatting/tests/ui-smoke.cjs 运行。
const { app, BrowserWindow } = require(require.resolve('electron', { paths: [process.cwd()] }));
const fs = require('node:fs');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
app.whenReady().then(async () => {
  const timer = setTimeout(() => app.exit(1), 60000);
  const win = new BrowserWindow({ show: false, width: 1520, height: 1200, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
  const js = code => win.webContents.executeJavaScript(code, true);
  const assert = (value, text) => { if (!value) throw Error(text); };
  async function ready() {
    for (let i = 0; i < 100; i++) { if (await js(`document.querySelectorAll('video').length === 2 && [...document.querySelectorAll('video')].every(v=>v.readyState>=2)`)) return; await wait(100); }
    throw Error('视频未就绪');
  }
  try {
    await win.loadURL('http://127.0.0.1:8765');
    await wait(800);
    assert(await js(`document.querySelector('h1').textContent==='让人像，走出背景。' && document.querySelector('input[type=color]').value==='#3b82f6' && document.querySelector('.device').textContent.includes('建议使用配备 NVIDIA 显卡的设备')`), '新版文案、默认蓝色及显卡建议');
    // Electron 使用独立浏览器存储，不修改用户浏览器中的当前任务。
    await js(`localStorage.removeItem('video-matting-current-job');document.querySelector('.remove-material').click();void 0;`);
    assert(await js(`(()=>{let count=0;const input=document.querySelector('input[type=file]');input.click=()=>count++;document.querySelector('.viewport.original').click();document.querySelector('.upload-overlay').click();return count===2})()`), '点击上传区域未打开选择器');
    await js(`[...document.querySelectorAll('.history button')].find(b=>b.querySelector('small')?.textContent.trim()==='已完成').click()`);
    await ready();
    const selectedName = await js(`document.querySelector('.workspace-head strong').textContent.trim()`);
    await wait(250);
    assert(await js(`[...document.querySelectorAll('.player-label')].every(e=>/\\d+\\.\\d{2} MB/.test(e.textContent)) && document.querySelector('.processing-time').textContent.includes('处理用时') && document.querySelector('.history-info em').textContent.includes('创建时间') && document.querySelector('.history-info em').textContent.includes('用时')`), '两侧 MB、下载旁用时及历史创建时间/用时');
    await win.reload();
    await wait(800);
    assert(await js(`document.querySelectorAll('video').length===0 && !document.querySelector('.export a.primary') && !document.querySelector('.phase-progress') && [...document.querySelectorAll('.history button:not(.clear-history)')].some(b=>b.querySelector('.history-info > span')?.textContent.trim()===${JSON.stringify(selectedName)})`), '刷新清空工作区且保留任务历史');
    await js(`[...document.querySelectorAll('.history button:not(.clear-history)')].find(b=>b.querySelector('.history-info > span')?.textContent.trim()===${JSON.stringify(selectedName)}).click()`);
    await ready();
    assert(await js(`(()=>{const box=document.querySelector('.result-viewport'),background=document.querySelector('.video-background').getBoundingClientRect(),v=document.querySelector('video');const scale=Math.min(box.clientWidth/v.videoWidth,box.clientHeight/v.videoHeight);return Math.abs(background.width-v.videoWidth*scale)<1 && Math.abs(background.height-v.videoHeight*scale)<1 && background.width<box.clientWidth && getComputedStyle(box).backgroundColor===getComputedStyle(document.querySelector('.original')).backgroundColor})()`), '结果背景只覆盖原视频画幅，留白区域为相同灰色');
    await js(`document.querySelector('video').click();void 0;`);
    await wait(200);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>!v.paused)`), '点击原视频同步播放');
    await js(`document.querySelectorAll('video')[1].click();void 0;`);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>v.paused)`), '点击结果视频同步暂停');
    assert(await js(`document.title==='清影工坊 · VideoMatting' && !document.querySelector('.phase-progress')`), '中文标题及完成后隐藏阶段进度');
    assert(await js(`(()=>{const a=document.querySelector('.export-modes').getBoundingClientRect(),b=document.querySelector('.backgrounds').getBoundingClientRect();return Math.abs(a.top-b.top)<10})()`), '导出模式和背景同一行');
    await js(`var slider=document.querySelector('input[type=range]');slider.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));slider.value='.4';slider.dispatchEvent(new Event('input',{bubbles:true}));void 0;`);
    await wait(300);
    const firstPreview = await js(`(()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=210;c.getContext('2d').drawImage(v,0,0,160,210);return c.toDataURL()})()`);
    await js(`slider.value='1.9';slider.dispatchEvent(new Event('input',{bubbles:true}));void 0;`);
    await wait(300);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>Math.abs(v.currentTime-1.9)<.05 && !v.seeking && v.paused)`), '未松开滑块时实时定位左右画面');
    assert(await js(`document.querySelector('.seek-wrap output').textContent.includes('1.90s')`), '拖柄上方显示时间');
    assert(await js(`document.querySelector('video').muted && document.querySelector('select[aria-label="导出音频"]').selectedIndex===0`), '默认静音播放与静音输出');
    const secondPreview = await js(`(()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=210;c.getContext('2d').drawImage(v,0,0,160,210);return c.toDataURL()})()`);
    assert(firstPreview!==secondPreview, '拖动期间画面应更新');
    await js(`window.dispatchEvent(new PointerEvent('pointerup'));slider.value='0';slider.dispatchEvent(new Event('input',{bubbles:true}));void 0;`);
    await wait(200);
    assert(await js(`document.querySelectorAll('.viewport-tools button').length===2 && [...document.querySelectorAll('.viewport-tools button')].every(b=>b.classList.contains('fullscreen-icon') && b.querySelector('svg')) && !document.querySelector('.transport').textContent.includes('回到开头') && document.querySelector('.transport button:last-child').textContent.trim()==='全屏对比'`), '普通预览只有全屏图标，移除回到开头');
    await js(`var speed=document.querySelector('select[aria-label="同步播放速度"]');speed.value='0.5';speed.dispatchEvent(new Event('change',{bubbles:true}));void 0;`);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>v.playbackRate===.5)`), '同步慢速');
    await js(`var speed=document.querySelector('select[aria-label="同步播放速度"]');speed.value='2';speed.dispatchEvent(new Event('change',{bubbles:true}));void 0;`);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>v.playbackRate===2)`), '同步倍速');
    await js(`var speed=document.querySelector('select[aria-label="同步播放速度"]');speed.value='1';speed.dispatchEvent(new Event('change',{bubbles:true}));void 0;`);
    assert(await js(`document.querySelector('.player-label').textContent.includes('832 × 1108') && document.querySelector('.copyright').textContent.includes('©2026 izcw')`), '分辨率与页脚');
    await js(`document.body.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true}));void 0;`);
    await wait(4500);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>!v.paused && v.loop) && Math.abs(document.querySelectorAll('video')[0].currentTime-document.querySelectorAll('video')[1].currentTime)<.15`), '循环与同步播放');
    await js(`document.body.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true}));void 0;`);
    assert(await js(`[...document.querySelectorAll('video')].every(v=>v.paused)`), '空格暂停');
    for (const target of ['.viewport.original', '.viewport:not(.original)', '.comparison']) {
      await js(`document.querySelector('${target}').requestFullscreen()`);
      // 隐藏 Electron 窗口不派发原生窗口变更事件，补发 DOM 通知验证状态渲染。
      await js(`document.dispatchEvent(new Event('fullscreenchange'));void 0;`);
      await wait(150);
      assert(await js(`document.fullscreenElement===document.querySelector('${target}')`), '全屏 '+target);
      await wait(100);
      if (target !== '.comparison') {
        assert(await js(`(()=>{const buttons=[...document.querySelector('${target}').querySelectorAll('.viewport-tools button')];return buttons.length===2 && buttons.some(b=>b.getAttribute('aria-label')==='关闭全屏') && buttons.some(b=>b.getAttribute('aria-label')==='播放' && b.querySelector('svg') && !b.textContent.trim())})()`), '单独全屏显示播放与关闭图标');
      } else {
        assert(await js(`!document.querySelector('.viewport-tools') && !document.querySelector('.upload-overlay') && [...document.querySelectorAll('.transport button')].some(b=>b.textContent.trim()==='关闭')`), '对比全屏隐藏单侧控制和更换视频');
      }
      assert(await js(`!document.querySelector('.upload-overlay') && [...document.querySelectorAll('.speed-control')].every(e=>!e.textContent.includes('速度'))`), '任何全屏均隐藏更换视频且不显示速度文字');
      await js(`document.querySelector('${target}').querySelector('${target === '.comparison' ? '.transport button:last-child' : '.fullscreen-icon'}').focus();void 0;`);
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Space'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Space'});
      await wait(300);
      assert(await js(`!!document.fullscreenElement && [...document.querySelectorAll('video')].every(v=>!v.paused)`), '全屏按钮有焦点时空格播放而非关闭');
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Space'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Space'});
      await wait(300);
      assert(await js(`!!document.fullscreenElement && [...document.querySelectorAll('video')].every(v=>v.paused)`), '空格暂停且保留全屏 '+target+' '+JSON.stringify(await js(`({full:!!document.fullscreenElement,paused:[...document.querySelectorAll('video')].map(v=>v.paused),focus:document.activeElement?.outerHTML})`)));
      await js(`document.querySelector('${target}').querySelector('${target === '.comparison' ? '.transport button:last-child' : '.viewport-tools button:last-child'}').click();void 0;`);
      await wait(100);
      assert(await js(`!document.fullscreenElement`), '关闭全屏');
    }
    await js(`[...document.querySelectorAll('.backgrounds button')].find(b=>b.textContent.trim()==='绿幕').click()`);
    assert(await js(`document.querySelector('.video-background.green')!==null`), '绿幕预览');
    for (const mode of ['自定义背景', 'Alpha Matte', '透明 WebM']) {
      await js(`[...document.querySelectorAll('.export-modes button')].find(b=>b.textContent.trim()==='${mode}').click()`);
      for (let i=0;i<240;i++) { if (await js(`!!document.querySelector('.export a.primary')`)) break; await wait(100); }
      await ready();
      assert(await js(`${mode === 'Alpha Matte' ? '!' : '!!'}document.querySelector('.backgrounds')`), 'Alpha Matte 隐藏背景选项');
      assert(await js(`!!document.querySelector('.export a.primary') && !document.querySelector('.error')`), '导出失败 '+mode);
      const downloaded = await js(`document.querySelector('.export a.primary').href`);
      const response = await js(`fetch(${JSON.stringify(downloaded)}).then(r=>({ok:r.ok,type:r.headers.get('content-type'),disposition:r.headers.get('content-disposition')}))`);
      assert(response.ok && response.disposition.includes('attachment') && response.type.includes(mode==='透明 WebM'?'webm':'mp4'), '下载格式 '+mode);
    }
    const dropped = await js(`(async()=>{const jobs=await fetch('/api/jobs').then(r=>r.json());const done=jobs.find(j=>j.status==='done');window.uploadCalls=0;const Real=XMLHttpRequest;class Fake extends Real {open(){}send(form){window.uploadCalls++;this.upload.onprogress?.({lengthComputable:true,loaded:5,total:10});Object.defineProperty(this,'status',{value:202});Object.defineProperty(this,'responseText',{value:JSON.stringify(done)});setTimeout(()=>this.onload(),350)}}window.XMLHttpRequest=Fake;const data=new DataTransfer();data.items.add(new File(['test'],'drop.mp4',{type:'video/mp4'}));document.querySelector('.viewport.original').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:data}));return true})()`);
    await wait(80);
    assert(await js(`document.querySelector('.upload-progress progress').value===50 && document.querySelector('.upload-progress').textContent.includes('50%')`), '上传过程中独立进度条及实时百分比');
    await wait(450);
    assert(dropped && await js(`window.uploadCalls===1`), '拖放上传');
    await ready();
    fs.writeFileSync(process.env.TEMP + '/video-matting-updated.png', (await win.webContents.capturePage()).toPNG());
    await js(`document.querySelector('.remove-material').click()`);
    assert(await js(`document.querySelectorAll('video').length===0 && !document.querySelector('.upload-overlay').disabled`), '删除当前素材');
    await js(`(async()=>{window.realFetch=fetch;const records=await fetch('/api/jobs').then(r=>r.json());window.mockProgress={...records.find(j=>j.status==='done'),status:'processing',stage:'逐帧精细抠像',progress:37,completed_frames:20,total_frames:100,started_at:Date.now()/1000-30};window.fetch=(path,options)=>String(path)==='/api/jobs/'+window.mockProgress.id?Promise.resolve(new Response(JSON.stringify(window.mockProgress),{headers:{'content-type':'application/json'}})):window.realFetch(path,options);[...document.querySelectorAll('.history button')].find(b=>b.querySelector('small')?.textContent.trim()==='已完成').click()})()`);
    await wait(300);
    assert(await js(`document.querySelector('.phase-progress').textContent.includes('20/100 张 · 20%')`), '显示实时逐帧计数');
    assert(await js(`(()=>{const complete=document.querySelector('.phase-progress .complete'),active=document.querySelector('.phase-progress .active');return getComputedStyle(complete).backgroundColor==='rgb(98, 90, 214)' && getComputedStyle(complete.querySelector('span')).color==='rgb(255, 255, 255)' && getComputedStyle(complete.querySelector('small')).color==='rgb(255, 255, 255)' && getComputedStyle(active).backgroundColor==='rgb(239, 240, 243)' && getComputedStyle(active).borderRadius==='12px'})()`), '圆角灰色阶段卡片，完成后紫底白字');
    assert(await js(`document.querySelector('.clear-history').disabled`), '处理中不允许清空记录');
    await js(`window.mockProgress.completed_frames=35;window.mockProgress.progress=46;void 0;`);
    await wait(1200);
    assert(await js(`document.querySelector('.phase-progress').textContent.includes('35/100 张 · 35%')`), '逐帧计数随轮询更新');
    await js(`window.mockProgress.status='done';window.mockProgress.progress=100;void 0;`);
    await wait(1200);
    assert(await js(`!document.querySelector('.phase-progress')`), '完成后移除阶段进度');
    await js(`window.fetch=window.realFetch;void 0;`);
    await js(`window.confirm=()=>false;document.querySelector('.clear-history').click();void 0;`);
    assert(await js(`document.querySelectorAll('.history button:not(.clear-history)').length>0`), '取消确认后保留历史');
    await js(`window.clearCalls=0;window.confirm=message=>{window.clearPrompt=message;return true};window.fetch=(path,options)=>{if(String(path)==='/api/history/clear'){window.clearCalls++;return Promise.resolve(new Response(JSON.stringify({cleared:5,files_deleted:false}),{headers:{'content-type':'application/json'}}))}if(String(path)==='/api/jobs')return Promise.resolve(new Response('[]',{headers:{'content-type':'application/json'}}));return window.realFetch(path,options)};document.querySelector('.clear-history').click();void 0;`);
    await wait(150);
    assert(await js(`window.clearCalls===1 && window.clearPrompt.includes('不删除') && !document.querySelector('.history button:not(.clear-history)') && document.querySelector('.history-message').textContent.includes('已清空 5 条记录') && document.querySelectorAll('video').length===2`), '确认清空后移除列表并保留当前预览');
    await js(`(async()=>{const records=await window.realFetch('/api/jobs').then(r=>r.json());window.resumeRecord={...records.find(j=>j.status==='done'),status:'interrupted',can_resume:true,stage:'任务已中断'};window.resumeCalls=0;window.fetch=(path,options)=>{path=String(path);if(path==='/api/jobs')return Promise.resolve(new Response(JSON.stringify([window.resumeRecord])));if(path==='/api/jobs/'+window.resumeRecord.id+'/resume'){window.resumeCalls++;window.resumeRecord={...window.resumeRecord,status:'queued',can_resume:false,stage:'恢复处理'};return Promise.resolve(new Response(JSON.stringify(window.resumeRecord)))}if(path==='/api/jobs/'+window.resumeRecord.id)return Promise.resolve(new Response(JSON.stringify(window.resumeRecord)));return window.realFetch(path,options)};document.querySelector('.remove-material').click();const data=new DataTransfer();data.items.add(new File(['test'],'resume.mp4',{type:'video/mp4'}));document.querySelector('.viewport.original').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:data}))})()`);
    await wait(650);
    await js(`document.querySelector('.history button:not(.clear-history)').click();void 0;`);
    await wait(150);
    assert(await js(`window.resumeCalls===1 && document.querySelector('.badge').textContent.includes('排队')`), '点击中断任务恢复处理');
    await js(`document.querySelector('.history button:not(.clear-history)').click();void 0;`);
    await wait(150);
    assert(await js(`window.resumeCalls===1`), '重复点击未完成任务只连接进度，不重复启动');
    console.log('拖动实时画面、实时帧数及进度、中文标题、同排布局、Alpha 隐藏背景，以及原有交互：通过');
    clearTimeout(timer); app.exit(0);
  } catch (error) { console.error(error); clearTimeout(timer); app.exit(1); }
});
