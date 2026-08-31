// ── Save/Load Adapter (TradingView chart layout persistence) ──────────────────

export function createSaveLoadAdapter() {
  return {
    async getAllCharts() {
      const res = await fetch('/api/charts');
      return await res.json();
    },
    async removeChart(id) {
      await fetch(`/api/charts/${id}`, { method: 'DELETE' });
    },
    async saveChart(chartData) {
      // Strip transient shapes (execution arrows, programmatic trend lines) from
      // the chart layout before saving. These are redrawn from live data on every
      // page load, so persisting them causes stale duplicates on reload.
      let content = chartData.content;
      try {
        const layout = JSON.parse(content);
        if (layout.charts) {
          layout.charts.forEach(chart => {
            (chart.panes || []).forEach(pane => {
              if (pane.sources) {
                pane.sources = pane.sources.filter(
                  s => s.type !== 'LineToolFlagMark' && s.type !== 'LineToolTrendLine'
                );
              }
            });
          });
          content = JSON.stringify(layout);
        }
      } catch (e) {
        console.warn('[SaveChart] Failed to strip transient shapes:', e);
      }
      const res = await fetch('/api/charts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: chartData.id,
          name: chartData.name,
          symbol: chartData.symbol,
          resolution: chartData.resolution,
          content,
          timestamp: Math.floor(Date.now() / 1000),
        }),
      });
      const data = await res.json();
      return data.id;
    },
    async getChartContent(chartId) {
      const res = await fetch(`/api/charts/${chartId}`);
      const data = await res.json();
      return data.content;
    },
    async getAllStudyTemplates() {
      const res = await fetch('/api/study_templates');
      return await res.json();
    },
    async removeStudyTemplate(info) {
      await fetch(`/api/study_templates/${encodeURIComponent(info.name)}`, { method: 'DELETE' });
    },
    async saveStudyTemplate(data) {
      await fetch('/api/study_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: data.name, content: data.content }),
      });
    },
    async getStudyTemplateContent(info) {
      const res = await fetch(`/api/study_templates/${encodeURIComponent(info.name)}`);
      const data = await res.json();
      return data.content;
    },
    async getDrawingTemplates(toolName) {
      const res = await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}`);
      return await res.json();
    },
    async loadDrawingTemplate(toolName, templateName) {
      const res = await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}/${encodeURIComponent(templateName)}`);
      const data = await res.json();
      return data.content;
    },
    async removeDrawingTemplate(toolName, templateName) {
      await fetch(`/api/drawing_templates/${encodeURIComponent(toolName)}/${encodeURIComponent(templateName)}`, { method: 'DELETE' });
    },
    async saveDrawingTemplate(toolName, templateName, content) {
      await fetch('/api/drawing_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_name: toolName, template_name: templateName, content }),
      });
    },
    async getAllChartTemplates() {
      const res = await fetch('/api/chart_templates');
      return await res.json();
    },
    async saveChartTemplate(templateName, content) {
      await fetch('/api/chart_templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: templateName, content }),
      });
    },
    async removeChartTemplate(templateName) {
      await fetch(`/api/chart_templates/${encodeURIComponent(templateName)}`, { method: 'DELETE' });
    },
    async getChartTemplateContent(templateName) {
      const res = await fetch(`/api/chart_templates/${encodeURIComponent(templateName)}`);
      return await res.json();
    },
  };
}
