
document.getElementById('restart').addEventListener('click', () => {
  // 发送消息给 content-script，要求重新启动监听
	chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
		const activeTab = tabs[0];
		chrome.tabs.sendMessage(activeTab.id, { action: 'restartListener' });
	});
});



document.getElementById('export').addEventListener('click', () => {
	console.log("export click")
	// 	chrome.runtime.sendMessage({
	//   	type: 'SAVE_FILE',
	//   	filename: 'scroll_catcher.txt'
	// });
	// console.log("send message")

	/**
	* 把一段原始日志字符串 → {key: value} 对象
	*/
	function parseOneLog(raw) {
		const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);

		// 第一行包含时间戳和方向
		const head = lines[0];
		const [, ts, direction] = head.match(/^\[(.+?)\]:\s*(.+?)\s+detail:/i) || [];
		const obj = { timestamp: ts, direction };

		// 其余行按 ":" 切分键值
		for (let i = 1; i < lines.length; i++) {
			const [k, v] = lines[i].split(':').map(s => s.trim());
			if (k && v !== undefined) obj[k] = v;
		}
		return obj;
	}

	/**
	* 把多条日志对象 → CSV 字符串
	*/
	function logsToCSV(logArr) {
		if (!logArr.length) return '';

		// 所有可能的列（按出现顺序）
		const allKeys = Array.from(
			new Set(logArr.flatMap(o => Object.keys(o)))
			);

		// 表头
		const csvRows = [allKeys.join(',')];

		// 每一行
		for (const obj of logArr) {
			const row = allKeys.map(k => {
				const v = obj[k] ?? '';
		    // 如有逗号/引号/换行，做 CSV 转义
				const needQuote = /[",\n]/.test(v);
				const escaped = v.replace(/"/g, '""');
				return needQuote ? `"${escaped}"` : escaped;
			});
			csvRows.push(row.join(','));
		}

		return csvRows.join('\n');
	}

	// 定义一个函数来提取每条日志的第一行
	function getFirstLine(log) {
		return log.split('\n')[0];
	}


	chrome.storage.local.get(['scrollData'], res => {

	const scrollData = res.scrollData || [];
	// 使用 Array.prototype.sort 方法按第一行排序
	scrollData.sort((a, b) => {
		const firstLineA = getFirstLine(a);
		const firstLineB = getFirstLine(b);
		return firstLineA.localeCompare(firstLineB);
	});

	const parsed = scrollData.map(parseOneLog);   // 转成对象数组
	const csv = logsToCSV(parsed); 

	const blob = new Blob([csv], {
		type: 'text/csv;charset=utf-8'
	});
	const url = URL.createObjectURL(blob);
	chrome.downloads.download({ url, filename: 'scroll_catcher.csv' });
	});
});

document.getElementById('clear').addEventListener('click', () => {
	chrome.storage.local.remove('scrollData');
  // 发送消息给 content-script，要求清理缓存
	chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
		const activeTab = tabs[0];
		chrome.tabs.sendMessage(activeTab.id, { action: 'clear' });
	});
});

