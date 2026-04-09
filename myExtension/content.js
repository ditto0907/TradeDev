
// 3. 全局变量
let seen = new Set();        // 只存第一行
let buffer = [];             // 真正去重后的结果



// 2. 初始化监听
let currentObserver


(() => {

  function waitFor(selector, callback) {
    const timer = setInterval(() => {
      const node = document.querySelector(selector);
      if (node) {
        clearInterval(timer);
        callback(node);
      }
    }, 300);
  }

  function getKey(str) {
    return str.split('\n')[0];   // 第一行
  }

  function addLog(rawText) {
    const key = getKey(rawText);
    if (seen.has(key)) return;   // 已存在，跳过
    seen.add(key);
    buffer.push(rawText);
  }

  // 3. 启动 MutationObserver
  function startObserve() {

    const root = document.querySelector('[class*="virtualScroll"]');
    console.log("root: ",root); 

    if (!root) return;

    const mo = new MutationObserver(muts => {
      muts.forEach(m => {
        console.log("muts:",m); 
        m.addedNodes.forEach(n => {
          console.log("Node:",n); 
          if (n.nodeType === Node.ELEMENT_NODE) {
            span = n.querySelector('.msg-zsZSd11H')
            txt = span.textContent.trim()
            
            const key = txt.split('\n')[0];
            if (!seen.has(key)) {
              seen.add(key);
              buffer.push(txt);
              console.log("Text:",txt); 
            }
            
          }
        });
      });
      // 实时写回 storage（你也可以改为定时批写）
      chrome.storage.local.set({ scrollData: buffer });
    });

    mo.observe(root, { childList: true, subtree: true });
    return mo;
  }

  waitFor('.virtualScroll-L0IhqRpX', root => {
    console.log('✅ root 已找到', root);
    // 4. 延迟启动，确保 DOM 已渲染
    currentObserver = startObserve();
  })

  // 3. 接收 popup 发来的消息
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    
    if (message.action === 'restartListener') {
      console.log('收到重新启动监听的指令');
      // 停止当前监听
      if (currentObserver) {
        currentObserver.disconnect();
      }
      // 重新启动监听
      currentObserver = startObserve();
    }

    if (message.action === 'clear') {
      console.log('收到清理缓存的指令');
      seen.clear();        
      buffer = [];             
      console.log('缓存清理完成');
      alert('已清空！');

    }


  });

})();












