chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  
  console.log("receive message",msg);
  if (msg.type === 'SAVE_FILE') {
  	
  	// chrome.storage.local.get(['scrollData'], res => {
    	
    	// const blob = new Blob([res.scrollData?.join('\n\n') || ''], {
     //  		type: 'text/plain;charset=utf-8'
     //  	});
    	// const url = URL.createObjectURL(blob);
    	// chrome.downloads.download({ url, filename: 'scroll_catcher.txt' });

	    // const blob = new Blob([msg.data], { type: 'text/plain' });
	    // const url = URL.createObjectURL(blob);
	    // chrome.downloads.download({ url, filename: msg.filename });
	// });
  }
});
