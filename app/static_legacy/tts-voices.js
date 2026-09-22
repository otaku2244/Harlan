/* Both chat and theater use the same route/voice picker and saved voice IDs. */
(function(root) {
  function populate(select, voices, selected) {
    const doc = select.ownerDocument;
    select.replaceChildren();
    const groups = new Map();
    const addOption = (parent, value, label) => {
      const option = doc.createElement('option');
      option.value = value;
      option.textContent = label;
      parent.appendChild(option);
    };
    for (const voice of voices || []) {
      const id = voice.uri || voice.customized_model_id || voice.name;
      if (!id) continue;
      const route = id.startsWith('edge:') ? 'Edge 免费' : id.startsWith('minimax:') ? 'MiniMax' : '硅基流动';
      if (!groups.has(route)) {
        const group = doc.createElement('optgroup');
        group.label = route;
        groups.set(route, group);
        select.appendChild(group);
      }
      const name = voice.customName || voice.name || id;
      addOption(groups.get(route), id, name.startsWith(route + ' · ') ? name : route + ' · ' + name);
    }
    if (selected && !Array.from(select.options).some(o => o.value === selected)) {
      if (selected.startsWith('edge:')) {
        // Deliberately removed from favorites: clear the selection, never switch to paid TTS.
        addOption(select, '', '请先试听并选择音色');
        select.value = '';
        return '';
      }
      // A temporary provider outage must not silently change the chosen voice/route.
      addOption(select, selected, '当前音色（列表暂不可用）');
    }
    if (!select.options.length) addOption(select, '', '无可用音色');
    select.value = selected || select.options[0].value;
    return select.value;
  }
  root.TTSVoices = { populate };
  if (typeof module !== 'undefined') module.exports = root.TTSVoices;
})(typeof window !== 'undefined' ? window : globalThis);
