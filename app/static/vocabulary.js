let currentPage = 1;
const pageSize = 20;
let allUnits = [];
let vocabularyGroups = [];
let allVocabularies = [];
let polishVoices = [];
let currentVoicePreviewUrl = null;
const statusTimers = {};
const VALID_LEVELS = new Set(['all', 'weak', 'learning', 'learned']);
let suppressUrlSync = false;
let lastQueryString = '';
let lastServerTotal = 0;
let lastServerPages = 1;

function clearStatus(targetId) {
    const container = document.getElementById(targetId);
    if (!container) return;

    if (statusTimers[targetId]) {
        clearTimeout(statusTimers[targetId]);
        delete statusTimers[targetId];
    }

    container.textContent = '';
    container.classList.add('hidden');
    container.classList.remove('success', 'error', 'warning', 'info');
}

function setStatus(targetId, message, variant = 'info', options = {}) {
    const container = document.getElementById(targetId);
    if (!container) return;

    const { persist = false } = options;

    if (statusTimers[targetId]) {
        clearTimeout(statusTimers[targetId]);
        delete statusTimers[targetId];
    }

    container.textContent = message;
    container.classList.remove('hidden', 'success', 'error', 'warning', 'info');
    container.classList.add(variant);

    if (!persist) {
        statusTimers[targetId] = setTimeout(() => {
            clearStatus(targetId);
        }, 4500);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function updateVocabularySummary({ shown = 0, total = 0, page = 1, pages = 1 } = {}) {
    const summary = document.getElementById('vocab-results-summary');
    if (!summary) return;
    summary.textContent = `${shown} shown on this page · ${total} total · page ${page}/${pages}`;
}

function getSelectedLabel(selectId) {
    const select = document.getElementById(selectId);
    if (!select) return '';
    const selected = select.options[select.selectedIndex];
    if (!selected) return '';

    const label = selected.textContent.trim();
    if (selectId === 'vocab-group') {
        return label.replace(/\s*\(\d+\s+files?\)\s*$/i, '');
    }
    return label;
}

function renderActiveFilterChips() {
    const chipsContainer = document.getElementById('vocab-active-filters');
    if (!chipsContainer) return;

    const state = getUIState();
    const chips = [];

    if (state.group !== 'all') {
        chips.push({
            key: 'group',
            label: `Group: ${getSelectedLabel('vocab-group') || state.group}`,
        });
    }

    if (state.source !== 'all') {
        chips.push({
            key: 'source',
            label: `Source: ${state.source}`,
        });
    }

    if (state.level !== 'all') {
        const levelLabel = state.level === 'weak'
            ? 'Weak'
            : state.level === 'learning'
                ? 'Learning'
                : 'Learned';
        chips.push({
            key: 'level',
            label: `Level: ${levelLabel}`,
        });
    }

    if (state.search) {
        chips.push({
            key: 'search',
            label: `Search: "${state.search}"`,
        });
    }

    if (chips.length === 0) {
        chipsContainer.innerHTML = '';
        chipsContainer.classList.add('hidden');
        return;
    }

    chipsContainer.classList.remove('hidden');
    chipsContainer.innerHTML = chips.map((chip) => (
        `<span class="filter-chip">`
        + `<span>${escapeHtml(chip.label)}</span>`
        + `<button type="button" class="filter-chip-clear" data-filter-key="${chip.key}" aria-label="Clear ${chip.key} filter">×</button>`
        + `</span>`
    )).join('');
}

function clearFilterChip(filterKey) {
    const groupSelect = document.getElementById('vocab-group');
    const sourceSelect = document.getElementById('vocab-source');
    const levelSelect = document.getElementById('vocab-filter');
    const searchInput = document.getElementById('vocab-search');

    if (filterKey === 'group' && groupSelect) {
        const sourceBefore = sourceSelect?.value || 'all';
        groupSelect.value = 'all';
        updateVocabularyDropdown('all');
        if (sourceSelect && sourceBefore !== 'all' && hasOptionValue(sourceSelect, sourceBefore)) {
            sourceSelect.value = sourceBefore;
        }
    } else if (filterKey === 'source' && sourceSelect) {
        sourceSelect.value = 'all';
    } else if (filterKey === 'level' && levelSelect) {
        levelSelect.value = 'all';
    } else if (filterKey === 'search' && searchInput) {
        searchInput.value = '';
    } else {
        return;
    }

    currentPage = 1;
    renderActiveFilterChips();
    syncQueryFromUI('push');
    loadVocabulary();
}

function hasOptionValue(select, value) {
    if (!select) return false;
    return Array.from(select.options).some((option) => option.value === value);
}

function parseQueryState() {
    const params = new URLSearchParams(window.location.search);

    const rawGroup = params.get('group');
    const normalizedGroup = (rawGroup && rawGroup.trim()) || 'all';
    const group = (normalizedGroup === 'all' || normalizedGroup === 'ungrouped' || /^\d+$/.test(normalizedGroup))
        ? normalizedGroup
        : 'all';

    const rawSource = params.get('source');
    const source = (rawSource && rawSource.trim()) || 'all';

    const rawLevel = params.get('level');
    const level = VALID_LEVELS.has(rawLevel) ? rawLevel : 'all';

    const search = (params.get('search') || '').trim();

    const pageNum = parseInt(params.get('page') || '1', 10);
    const page = Number.isNaN(pageNum) || pageNum < 1 ? 1 : pageNum;

    return { group, source, level, search, page };
}

function getUIState() {
    const groupSelect = document.getElementById('vocab-group');
    const sourceSelect = document.getElementById('vocab-source');
    const levelSelect = document.getElementById('vocab-filter');
    const searchInput = document.getElementById('vocab-search');

    const group = groupSelect?.value || 'all';
    const source = sourceSelect?.value || 'all';
    const level = VALID_LEVELS.has(levelSelect?.value) ? levelSelect.value : 'all';
    const search = (searchInput?.value || '').trim();
    const page = Number.isInteger(currentPage) && currentPage > 0 ? currentPage : 1;

    return { group, source, level, search, page };
}

function buildQueryStringFromState(state) {
    const params = new URLSearchParams();
    params.set('group', state.group || 'all');
    params.set('source', state.source || 'all');
    params.set('level', VALID_LEVELS.has(state.level) ? state.level : 'all');
    if (state.search) {
        params.set('search', state.search);
    }
    params.set('page', String(state.page && state.page > 0 ? state.page : 1));
    return params.toString();
}

function syncQueryFromUI(mode = 'replace') {
    if (suppressUrlSync) return;

    const state = getUIState();
    const query = buildQueryStringFromState(state);
    if (query === lastQueryString) return;

    const nextUrl = `${window.location.pathname}?${query}`;
    if (mode === 'push') {
        window.history.pushState(state, '', nextUrl);
    } else {
        window.history.replaceState(state, '', nextUrl);
    }

    lastQueryString = query;
}

function applyQueryStateToControls(state) {
    const groupSelect = document.getElementById('vocab-group');
    const sourceSelect = document.getElementById('vocab-source');
    const levelSelect = document.getElementById('vocab-filter');
    const searchInput = document.getElementById('vocab-search');

    let group = state.group;
    let source = state.source;
    let level = VALID_LEVELS.has(state.level) ? state.level : 'all';
    const search = state.search || '';

    if (groupSelect) {
        if (!hasOptionValue(groupSelect, group)) {
            group = 'all';
        }
        groupSelect.value = group;
        updateVocabularyDropdown(group);
    }

    if (sourceSelect) {
        if (source !== 'all' && !hasOptionValue(sourceSelect, source)) {
            updateVocabularyDropdown('all');
            if (groupSelect) {
                groupSelect.value = 'all';
            }
            group = 'all';
        }

        if (!hasOptionValue(sourceSelect, source)) {
            source = 'all';
        }
        sourceSelect.value = source;
    }

    if (levelSelect) {
        level = hasOptionValue(levelSelect, level) ? level : 'all';
        levelSelect.value = level;
    }

    if (searchInput) {
        searchInput.value = search;
    }

    currentPage = state.page && state.page > 0 ? state.page : 1;
    renderActiveFilterChips();
    return {
        group,
        source,
        level,
        search,
        page: currentPage,
    };
}

async function loadVocabularyGroups() {
    try {
        const [groupsResp, vocabsResp] = await Promise.all([
            fetch('/api/vocabulary-groups'),
            fetch('/api/vocabularies'),
        ]);

        if (!groupsResp.ok || !vocabsResp.ok) {
            throw new Error('Failed to load groups or vocabularies');
        }

        vocabularyGroups = await groupsResp.json();
        allVocabularies = await vocabsResp.json();

        const groupSelect = document.getElementById('vocab-group');
        if (groupSelect) {
            groupSelect.innerHTML = '<option value="all">All Groups</option>';

            vocabularyGroups.forEach((group) => {
                if (group.id) {
                    const option = document.createElement('option');
                    option.value = group.id;
                    option.textContent = `${group.name} (${group.vocabulary_count} files)`;
                    groupSelect.appendChild(option);
                }
            });

            const ungrouped = vocabularyGroups.find((group) => group.id === null);
            if (ungrouped && ungrouped.vocabulary_count > 0) {
                const option = document.createElement('option');
                option.value = 'ungrouped';
                option.textContent = `Ungrouped (${ungrouped.vocabulary_count} files)`;
                groupSelect.appendChild(option);
            }
        }

        updateVocabularyDropdown('all');
        populateAddFormGroupDropdown();
        updateAddFormVocabularyDropdown('all');
        clearStatus('browse-status');
        clearStatus('add-status');
    } catch (error) {
        console.error('Failed to load groups:', error);
        setStatus('browse-status', `Could not load group filters: ${error.message}`, 'warning', { persist: true });
        setStatus('add-status', 'Could not load vocabulary groups. You can still add to default vocabulary.', 'warning');
        await loadVocabularies();
    }
}

function updateVocabularyDropdown(groupId) {
    const vocabSelect = document.getElementById('vocab-source');
    if (!vocabSelect) return;

    let vocabularies = [];

    if (groupId === 'all') {
        vocabularies = allVocabularies;
    } else if (groupId === 'ungrouped') {
        vocabularies = allVocabularies.filter((vocab) => vocab.group_id === null);
    } else {
        const groupIdNum = parseInt(groupId, 10);
        vocabularies = allVocabularies.filter((vocab) => vocab.group_id === groupIdNum);
    }

    vocabularies.sort((a, b) => a.name.localeCompare(b.name));

    vocabSelect.innerHTML = '<option value="all">All Vocabularies</option>';
    vocabularies.forEach((vocab) => {
        const option = document.createElement('option');
        option.value = vocab.name;
        option.textContent = vocab.name;
        vocabSelect.appendChild(option);
    });
}

function populateAddFormGroupDropdown() {
    const addGroupSelect = document.getElementById('add-vocab-group');
    if (!addGroupSelect) return;

    addGroupSelect.innerHTML = '<option value="all">(All groups)</option>';

    vocabularyGroups.forEach((group) => {
        if (group.id) {
            const option = document.createElement('option');
            option.value = group.id;
            option.textContent = group.name;
            addGroupSelect.appendChild(option);
        }
    });

    const ungrouped = vocabularyGroups.find((group) => group.id === null);
    if (ungrouped && ungrouped.vocabulary_count > 0) {
        const option = document.createElement('option');
        option.value = 'ungrouped';
        option.textContent = 'Ungrouped';
        addGroupSelect.appendChild(option);
    }
}

function updateAddFormVocabularyDropdown(groupId) {
    const select = document.getElementById('vocabulary-select');
    if (!select) return;

    const current = select.value;

    let vocabs = [];
    if (groupId === 'all') {
        vocabs = allVocabularies;
    } else if (groupId === 'ungrouped') {
        vocabs = allVocabularies.filter((vocab) => vocab.group_id === null);
    } else {
        const groupIdNum = parseInt(groupId, 10);
        vocabs = allVocabularies.filter((vocab) => vocab.group_id === groupIdNum);
    }

    vocabs = [...vocabs].sort((a, b) => a.name.localeCompare(b.name));

    select.innerHTML = '<option value="">(Auto: Chat Vocabulary)</option>' +
        vocabs.map((vocab) => `<option value="${vocab.id}">${escapeHtml(vocab.name)}</option>`).join('');

    const stillExists = Array.from(select.options).some((option) => option.value === current);
    select.value = stillExists ? current : '';
}

async function loadVocabularies() {
    const select = document.getElementById('vocabulary-select');
    if (!select) return;

    try {
        const response = await fetch('/api/vocabularies');
        if (!response.ok) throw new Error('Failed to load vocabularies');

        const vocabs = await response.json();
        allVocabularies = vocabs;

        const current = select.value;
        select.innerHTML = '<option value="">(Auto: Chat Vocabulary)</option>' +
            vocabs.map((vocab) => `<option value="${vocab.id}">${escapeHtml(vocab.name)}</option>`).join('');
        select.value = current;
    } catch (error) {
        console.warn('Failed to load vocabularies:', error);
        setStatus('add-status', 'Could not refresh vocabulary selector. Add to default vocabulary is still available.', 'warning');
    }
}

async function loadVocabulary(options = {}) {
    const { allowClampRetry = true, clampInfoMessage = '' } = options;
    try {
        const sourceFilter = document.getElementById('vocab-source')?.value || 'all';
        const groupFilter = document.getElementById('vocab-group')?.value || 'all';
        const searchTerm = document.getElementById('vocab-search')?.value.trim() || '';
        let url = `/api/units?page=${currentPage}&page_size=${pageSize}`;

        if (sourceFilter && sourceFilter !== 'all') {
            url += `&source_pdf=${encodeURIComponent(sourceFilter)}`;
        } else if (groupFilter && groupFilter !== 'all') {
            url += `&group_id=${encodeURIComponent(groupFilter)}`;
        }

        if (searchTerm) {
            url += `&search=${encodeURIComponent(searchTerm)}`;
        }

        const response = await fetch(url);
        if (!response.ok) throw new Error('Failed to load vocabulary');

        const data = await response.json();
        const serverPages = Math.max(1, data.pages || 1);
        const serverTotal = data.total || 0;

        if (currentPage > serverPages) {
            const previousPage = currentPage;
            currentPage = serverPages;
            lastServerTotal = serverTotal;
            lastServerPages = serverPages;
            syncQueryFromUI('replace');
            const nextClampInfoMessage = previousPage !== currentPage
                ? `Moved to last available page (${currentPage}).`
                : '';

            if (allowClampRetry) {
                await loadVocabulary({
                    allowClampRetry: false,
                    clampInfoMessage: nextClampInfoMessage,
                });
            } else {
                renderVocabulary({
                    items: [],
                    total: serverTotal,
                    page: currentPage,
                    pages: serverPages,
                });
                renderPagination({
                    pages: serverPages,
                    page: currentPage,
                });
                if (nextClampInfoMessage) {
                    setStatus('browse-status', nextClampInfoMessage, 'info');
                }
            }
            return;
        }

        allUnits = data.items;
        lastServerTotal = serverTotal;
        lastServerPages = serverPages;
        renderVocabulary(data);
        renderPagination(data);
        if (clampInfoMessage) {
            setStatus('browse-status', clampInfoMessage, 'info');
        } else {
            clearStatus('browse-status');
        }
    } catch (error) {
        const vocabList = document.getElementById('vocab-list');
        if (vocabList) {
            vocabList.innerHTML = '<p class="text-muted text-center">Error loading vocabulary list.</p>';
        }
        updateVocabularySummary({ shown: 0, total: 0, page: currentPage, pages: lastServerPages || 1 });
        setStatus('browse-status', `Could not load vocabulary list: ${error.message}`, 'error', { persist: true });
    }
}

function renderVocabulary(data) {
    const container = document.getElementById('vocab-list');
    const filter = document.getElementById('vocab-filter')?.value || 'all';
    if (!container) return;

    const units = data.items.filter((unit) => {
        const conf = unit.progress?.confidence_score || 0;
        if (filter === 'weak' && conf >= 0.5) return false;
        if (filter === 'learning' && (conf < 0.5 || conf >= 0.8)) return false;
        if (filter === 'learned' && conf < 0.8) return false;
        return true;
    });

    if (units.length === 0) {
        container.innerHTML = '<p class="text-muted text-center">No matching vocabulary found.</p>';
        updateVocabularySummary({
            shown: 0,
            total: data.total ?? lastServerTotal ?? 0,
            page: data.page ?? currentPage,
            pages: data.pages ?? lastServerPages ?? 1,
        });
        return;
    }

    container.innerHTML = units.map((unit) => {
        const conf = unit.progress?.confidence_score || 0;
        const confPercent = Math.round(conf * 100);
        let confClass = 'conf-weak';
        if (conf >= 0.8) confClass = 'conf-learned';
        else if (conf >= 0.5) confClass = 'conf-learning';

        const shortSource = unit.source_pdf
            ? unit.source_pdf.replace('.pdf', '').substring(0, 15)
            : '';

        return `
            <div class="vocab-item" data-id="${unit.id}">
                <div class="vocab-content">
                    <div class="vocab-text">${escapeHtml(unit.text)}</div>
                    <div class="vocab-translation">${escapeHtml(unit.translation)}</div>
                </div>
                <div class="vocab-meta">
                    <span class="vocab-confidence ${confClass}">${confPercent}%</span>
                    <span class="vocab-type">${unit.type}</span>
                    ${shortSource ? `<span class="vocab-source" title="${escapeHtml(unit.source_pdf)}">${escapeHtml(shortSource)}</span>` : ''}
                </div>
                <div class="vocab-actions">
                    <button onclick="openEdit(${unit.id})" class="btn-icon" title="Edit">✏️</button>
                    <button onclick="deleteUnit(${unit.id})" class="btn-icon btn-icon-danger" title="Delete">🗑️</button>
                </div>
            </div>
        `;
    }).join('');

    updateVocabularySummary({
        shown: units.length,
        total: data.total ?? lastServerTotal ?? units.length,
        page: data.page ?? currentPage,
        pages: data.pages ?? lastServerPages ?? 1,
    });
}

function renderPagination(data) {
    const container = document.getElementById('vocab-pagination');
    if (!container) return;

    if (data.pages <= 1) {
        container.innerHTML = '';
        return;
    }

    let html = '<div class="pagination">';

    if (currentPage > 1) {
        html += `<button onclick="goToPage(${currentPage - 1})" class="btn-small btn-secondary">← Prev</button>`;
    }

    html += `<span class="page-info">Page ${data.page} of ${data.pages}</span>`;

    if (currentPage < data.pages) {
        html += `<button onclick="goToPage(${currentPage + 1})" class="btn-small btn-secondary">Next →</button>`;
    }

    html += '</div>';
    container.innerHTML = html;
}

function goToPage(page) {
    currentPage = Math.max(1, page);
    syncQueryFromUI('push');
    loadVocabulary();
}

async function loadPolishVoices() {
    try {
        const response = await fetch('/api/audio/voices/polish');
        if (!response.ok) throw new Error('Failed to load voices');

        polishVoices = await response.json();
        const select = document.getElementById('voice-select');
        if (!select) return;

        const defaultVoice = polishVoices.find((voice) => voice.is_default);
        const defaultLabel = defaultVoice
            ? `Default (${defaultVoice.display_name})`
            : 'Default (use global setting)';

        select.innerHTML = `<option value="">${defaultLabel}</option>` +
            polishVoices.map((voice) => `<option value="${voice.id}">${voice.display_name}</option>`).join('');
    } catch (error) {
        console.error('Failed to load Polish voices:', error);
        setStatus('edit-status', `Could not load voices: ${error.message}`, 'warning');
    }
}

function openEdit(unitId) {
    const unit = allUnits.find((item) => item.id === unitId);

    if (!unit) {
        setStatus('browse-status', 'Unit not found. Refresh the list and try again.', 'error');
        return;
    }

    document.getElementById('edit-unit-id').value = unitId;
    document.getElementById('edit-text').value = unit.text;
    document.getElementById('edit-translation').value = unit.translation;

    if (polishVoices.length === 0) {
        loadPolishVoices();
    }

    const voiceSection = document.getElementById('voice-section');
    if (voiceSection) {
        voiceSection.style.display = 'block';
    }

    document.getElementById('voice-select').value = '';
    document.getElementById('preview-audio').style.display = 'none';
    document.getElementById('preview-audio').src = '';
    currentVoicePreviewUrl = null;
    clearStatus('edit-status');

    document.getElementById('edit-modal').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('edit-modal').classList.add('hidden');

    const previewAudio = document.getElementById('preview-audio');
    if (currentVoicePreviewUrl) {
        URL.revokeObjectURL(currentVoicePreviewUrl);
        currentVoicePreviewUrl = null;
    }
    previewAudio.src = '';
    previewAudio.style.display = 'none';
    clearStatus('edit-status');
}

async function previewVoice() {
    const unitId = document.getElementById('edit-unit-id').value;
    const voiceId = document.getElementById('voice-select').value;

    if (!voiceId) {
        setStatus('edit-status', 'Select a voice first.', 'warning');
        return;
    }

    const previewBtn = document.getElementById('preview-voice-btn');
    const originalText = previewBtn.textContent;
    previewBtn.disabled = true;
    previewBtn.textContent = 'Generating...';
    setStatus('edit-status', 'Generating preview audio...', 'info', { persist: true });

    try {
        const response = await fetch(`/api/audio/${unitId}/override`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice: voiceId, confirm: false }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Failed to generate preview' }));
            throw new Error(errorData.detail || 'Failed to generate preview');
        }

        const audioBlob = await response.blob();
        if (currentVoicePreviewUrl) {
            URL.revokeObjectURL(currentVoicePreviewUrl);
        }
        currentVoicePreviewUrl = URL.createObjectURL(audioBlob);

        const previewAudio = document.getElementById('preview-audio');
        previewAudio.src = currentVoicePreviewUrl;
        previewAudio.style.display = 'block';
        previewAudio.play();

        setStatus('edit-status', 'Preview ready.', 'success');
    } catch (error) {
        setStatus('edit-status', `Error generating preview: ${error.message}`, 'error', { persist: true });
    } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = originalText;
    }
}

async function useVoice() {
    const unitId = document.getElementById('edit-unit-id').value;
    const voiceId = document.getElementById('voice-select').value;

    if (!voiceId) {
        setStatus('edit-status', 'Select a voice first.', 'warning');
        return;
    }

    if (!confirm('This will replace the pronunciation audio for this word. Continue?')) {
        return;
    }

    const useBtn = document.getElementById('use-voice-btn');
    const originalText = useBtn.textContent;
    useBtn.disabled = true;
    useBtn.textContent = 'Saving...';
    setStatus('edit-status', 'Saving voice override...', 'info', { persist: true });

    try {
        const response = await fetch(`/api/audio/${unitId}/override`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice: voiceId, confirm: true }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Failed to save voice override' }));
            throw new Error(errorData.detail || 'Failed to save voice override');
        }

        closeModal();
        setStatus('browse-status', 'Voice override saved successfully.', 'success');
    } catch (error) {
        setStatus('edit-status', `Error saving voice override: ${error.message}`, 'error', { persist: true });
    } finally {
        useBtn.disabled = false;
        useBtn.textContent = originalText;
    }
}

async function saveEdit(event) {
    event.preventDefault();

    const unitId = document.getElementById('edit-unit-id').value;
    const text = document.getElementById('edit-text').value.trim();
    const translation = document.getElementById('edit-translation').value.trim();

    try {
        const response = await fetch(`/api/units/${unitId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, translation }),
        });

        if (!response.ok) throw new Error('Failed to update');

        closeModal();
        setStatus('browse-status', 'Vocabulary updated.', 'success');
        await loadVocabulary();
    } catch (error) {
        setStatus('edit-status', `Error saving changes: ${error.message}`, 'error', { persist: true });
    }
}

async function deleteUnit(unitId) {
    console.log("DELETE CLICKED", unitId);
    if (!confirm('Are you sure you want to delete this vocabulary item?')) return;

    try {
        console.log("DELETE REQUEST", unitId);
        const response = await fetch(`/api/units/${unitId}`, { method: 'DELETE' });
        console.log("DELETE RESPONSE STATUS:", response.status);
        if (!response.ok) throw new Error('Failed to delete');

        setStatus('browse-status', 'Vocabulary item deleted.', 'success');
        await loadVocabulary();
    } catch (error) {
        setStatus('browse-status', `Error deleting item: ${error.message}`, 'error', { persist: true });
    }
}

async function deleteSelectedPdf() {
    const select = document.getElementById('source-files-select');
    if (!select || select.options.length === 0) {
        setStatus('cleanup-status', 'No source selected.', 'warning');
        return;
    }

    const selectedOption = select.options[select.selectedIndex];
    const filename = selectedOption.value;
    const count = selectedOption.dataset.count || '?';

    if (!confirm(`Are you sure you want to delete ALL ${count} units from "${filename}"?\n\nThis will remove all vocabulary and learning progress from this PDF.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/pdfs/${encodeURIComponent(filename)}`, {
            method: 'DELETE',
        });

        const data = await response.json().catch(() => ({}));

        if (response.ok) {
            setStatus('cleanup-status', `Deleted ${data.units_deleted || count} units from "${filename}".`, 'success', { persist: true });
            setTimeout(() => window.location.reload(), 500);
        } else {
            setStatus('cleanup-status', data.detail || 'Failed to delete source.', 'error', { persist: true });
        }
    } catch (error) {
        setStatus('cleanup-status', `Error deleting source: ${error.message}`, 'error', { persist: true });
    }
}

async function deletePdf(filename) {
    if (!confirm(`Are you sure you want to delete ALL units from "${filename}"?\n\nThis will remove all vocabulary and learning progress from this PDF.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/pdfs/${encodeURIComponent(filename)}`, {
            method: 'DELETE',
        });

        const data = await response.json().catch(() => ({}));

        if (response.ok) {
            setStatus('cleanup-status', `Deleted ${data.units_deleted || '?'} units from "${filename}".`, 'success', { persist: true });
            setTimeout(() => window.location.reload(), 500);
        } else {
            setStatus('cleanup-status', data.detail || 'Failed to delete source.', 'error', { persist: true });
        }
    } catch (error) {
        setStatus('cleanup-status', `Error deleting source: ${error.message}`, 'error', { persist: true });
    }
}

async function addWord(event) {
    event.preventDefault();

    const textInput = document.getElementById('new-text');
    const translationInput = document.getElementById('new-translation');
    const vocabSelect = document.getElementById('vocabulary-select');
    const btn = document.getElementById('add-btn') || document.querySelector('#add-word-form button[type="submit"]');
    const text = textInput.value.trim();
    const translation = translationInput.value.trim();
    const vocabValue = vocabSelect ? vocabSelect.value : '';

    let vocabularyId = null;
    if (vocabValue && vocabValue !== '') {
        const parsedId = parseInt(vocabValue, 10);
        if (!Number.isNaN(parsedId)) {
            vocabularyId = parsedId;
        }
    }

    if (!text || !translation) {
        setStatus('add-status', 'Enter both Polish text and translation.', 'warning');
        return;
    }

    try {
        if (btn) btn.disabled = true;

        const payload = {
            text,
            translation,
            vocabulary_id: vocabularyId,
        };

        const response = await fetch('/api/units', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        let data = {};
        try {
            data = await response.json();
        } catch (e) {}

        if (response.status === 409 && data.detail?.status === 'exists') {
            showAddMessage(`✔ Already exists in: ${data.detail.vocabulary_name}`);
            return;
        }

        if (!response.ok) {
            showAddMessage(data.detail || 'Error saving word');
            return;
        }

        textInput.value = '';
        translationInput.value = '';
        textInput.focus();
        showAddMessage('✔ Saved to Chat Vocabulary');
        document.getElementById('new-text').value = '';
        document.getElementById('new-translation').value = '';
        await loadVocabulary();
    } catch (error) {
        showAddMessage(`Error saving word: ${error.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

function showAddMessage(msg) {
    const el = document.getElementById("add-status");
    if (!el) return;

    el.textContent = msg;
    el.style.display = "block";
    el.style.opacity = "1";

    setTimeout(() => {
        el.style.opacity = "0.5";
    }, 2000);

    setTimeout(() => {
        el.style.display = "none";
    }, 4000);
}

async function initVocabularyPage() {
    const groupSelect = document.getElementById('vocab-group');
    if (groupSelect) {
        groupSelect.addEventListener('change', function handleGroupChange() {
            updateVocabularyDropdown(this.value);
            const sourceSelect = document.getElementById('vocab-source');
            if (sourceSelect) {
                sourceSelect.value = 'all';
            }
            currentPage = 1;
            renderActiveFilterChips();
            syncQueryFromUI('push');
            loadVocabulary();
        });
    }

    const addGroupSelect = document.getElementById('add-vocab-group');
    if (addGroupSelect) {
        addGroupSelect.addEventListener('change', function handleAddGroupChange() {
            updateAddFormVocabularyDropdown(this.value);
        });
    }

    const addTextInput = document.getElementById('new-text');
    const addTranslationInput = document.getElementById('new-translation');
    const addWordForm = document.getElementById('add-word-form');
    const chipsContainer = document.getElementById('vocab-active-filters');

    if (addTextInput && addTranslationInput) {
        addTextInput.addEventListener('keydown', (event) => {
            if (
                event.key !== 'Enter'
                || event.isComposing
                || event.shiftKey
                || event.metaKey
                || event.ctrlKey
                || event.altKey
            ) {
                return;
            }

            event.preventDefault();
            addTranslationInput.focus();
        });

        addTranslationInput.addEventListener('keydown', (event) => {
            if (
                event.key !== 'Enter'
                || event.isComposing
                || event.shiftKey
                || event.metaKey
                || event.ctrlKey
                || event.altKey
            ) {
                return;
            }

            event.preventDefault();
            if (addWordForm && typeof addWordForm.requestSubmit === 'function') {
                addWordForm.requestSubmit();
            } else if (addWordForm) {
                addWordForm.dispatchEvent(new Event('submit', { cancelable: true }));
            }
        });
    }

    if (chipsContainer) {
        chipsContainer.addEventListener('click', (event) => {
            const clearButton = event.target.closest('.filter-chip-clear');
            if (!clearButton) return;

            const filterKey = clearButton.dataset.filterKey;
            clearFilterChip(filterKey);
        });
    }

    let searchTimeout = null;
    const searchInput = document.getElementById('vocab-search');
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            if (searchTimeout) {
                clearTimeout(searchTimeout);
            }

            currentPage = 1;
            searchTimeout = setTimeout(() => {
                renderActiveFilterChips();
                syncQueryFromUI('replace');
                loadVocabulary();
            }, 300);
        });
    }

    const levelFilter = document.getElementById('vocab-filter');
    if (levelFilter) {
        levelFilter.addEventListener('change', () => {
            renderActiveFilterChips();
            syncQueryFromUI('push');
            renderVocabulary({
                items: allUnits,
                total: lastServerTotal,
                page: currentPage,
                pages: lastServerPages,
            });
        });
    }

    const sourceFilter = document.getElementById('vocab-source');
    if (sourceFilter) {
        sourceFilter.addEventListener('change', () => {
            currentPage = 1;
            renderActiveFilterChips();
            syncQueryFromUI('push');
            loadVocabulary();
        });
    }

    const editModal = document.getElementById('edit-modal');
    if (editModal) {
        editModal.addEventListener('click', (event) => {
            if (event.target.id === 'edit-modal') {
                closeModal();
            }
        });
    }

    window.addEventListener('popstate', () => {
        const parsedState = parseQueryState();
        suppressUrlSync = true;
        const effectiveState = applyQueryStateToControls(parsedState);
        suppressUrlSync = false;
        lastQueryString = buildQueryStringFromState(effectiveState);
        syncQueryFromUI('replace');
        loadVocabulary();
    });

    await loadVocabularyGroups();
    const initialEffectiveState = applyQueryStateToControls(parseQueryState());
    lastQueryString = buildQueryStringFromState(initialEffectiveState);
    syncQueryFromUI('replace');
    await loadVocabulary();
}

initVocabularyPage();
