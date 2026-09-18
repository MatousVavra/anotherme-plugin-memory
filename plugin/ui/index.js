function memoryPlugin() {
    return {
        facts: [],
        people: [],
        contradictions: [],
        filter: 'All',
        search: '',
        loading: true,
        editingId: null,
        editValue: '',
        bannerDismissed: false,
        showReview: false,
        resolvingId: null,
        expandedPerson: null,
        personDraft: null,
        personOriginal: '',
        savingPerson: false,

        async init() {
            await Promise.all([this.loadFacts(), this.loadPeople(), this.loadContradictions()]);
            this.loading = false;
            AM.onCleanup(() => {});
        },

        async loadFacts() {
            try {
                const resp = await AM.fetch('/plugins/memory/facts');
                if (!resp || !resp.ok) return;
                this.facts = await resp.json();
            } catch (e) { console.error('Memory loadFacts', e); }
        },

        async loadPeople() {
            try {
                const resp = await AM.fetch('/plugins/memory/people');
                if (!resp || !resp.ok) return;
                this.people = await resp.json();
            } catch (e) { console.error('Memory loadPeople', e); }
        },

        async loadContradictions() {
            try {
                const resp = await AM.fetch('/plugins/memory/contradictions');
                if (!resp || !resp.ok) return;
                this.contradictions = await resp.json();
            } catch (e) { console.error('Memory loadContradictions', e); }
        },

        activeFactFor(contradiction) {
            return this.facts.find(f => f.key === contradiction.key && !f.contradicted);
        },

        async resolveContradiction(contradiction, keep) {
            if (this.resolvingId !== null) return;
            this.resolvingId = contradiction.id;
            try {
                const action = keep === 'old' ? 'confirm' : 'dismiss';
                const resp = await AM.fetch('/plugins/memory/facts/' + contradiction.id + '/resolve?action=' + action, { method: 'POST' });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to resolve conflict', 'error');
                    return;
                }
                if (keep === 'old') {
                    const active = this.activeFactFor(contradiction);
                    if (active) {
                        const cleanup = await AM.fetch('/plugins/memory/facts/' + active.id + '/resolve?action=dismiss', { method: 'POST' });
                        if (!cleanup || !cleanup.ok) AM.toast('Failed to resolve conflict', 'error');
                    }
                }
                await Promise.all([this.loadFacts(), this.loadContradictions()]);
                AM.toast(keep === 'old' ? 'Kept previous memory' : 'Kept current memory', 'success');
            } catch (e) {
                AM.toast(e.message, 'error');
            } finally {
                this.resolvingId = null;
            }
        },

        openPerson(person) {
            if (this.expandedPerson === person.name) {
                this.expandedPerson = null;
                this.personDraft = null;
                this.personOriginal = '';
                return;
            }
            this.expandedPerson = person.name;
            this.personDraft = {
                relationship: person.relationship || '',
                notes: person.notes || '',
                tags: (person.tags || []).join(', '),
                pronunciation_hint: person.pronunciation_hint || '',
            };
            this.personOriginal = JSON.stringify(this.personDraft);
        },

        personDirty() {
            return !!this.personDraft && JSON.stringify(this.personDraft) !== this.personOriginal;
        },

        async savePerson() {
            if (!this.personDirty() || this.savingPerson) return;
            this.savingPerson = true;
            try {
                const resp = await AM.fetch('/plugins/memory/people/' + encodeURIComponent(this.expandedPerson), {
                    method: 'PUT',
                    body: {
                        relationship: this.personDraft.relationship,
                        notes: this.personDraft.notes,
                        tags: this.personDraft.tags.split(',').map(t => t.trim()).filter(Boolean),
                        pronunciation_hint: this.personDraft.pronunciation_hint,
                    },
                });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to save person', 'error');
                    return;
                }
                await this.loadPeople();
                this.personOriginal = JSON.stringify(this.personDraft);
                AM.toast('Person saved', 'success');
            } catch (e) {
                AM.toast(e.message, 'error');
            } finally {
                this.savingPerson = false;
            }
        },

        factCategory(fact) {
            return fact.category || this.categoriseFact(fact.key);
        },

        categories() {
            const seen = [];
            for (const fact of this.facts) {
                const cat = this.factCategory(fact);
                if (!seen.includes(cat)) seen.push(cat);
            }
            return seen.sort();
        },

        filteredFacts() {
            let list = this.facts;
            if (this.filter !== 'All') list = list.filter(f => this.factCategory(f) === this.filter);
            const q = this.search.trim().toLowerCase();
            if (q) list = list.filter(f => ((f.key || '') + ' ' + (f.value || '')).toLowerCase().includes(q));
            return list;
        },

        factSource(fact) {
            const source = 'From ' + (fact.source || 'manual');
            const when = AM.utils.formatDate(fact.updated_at || fact.created_at);
            const confidence = Math.round((fact.confidence != null ? fact.confidence : 1) * 100) + '%';
            return source + ' \u00b7 ' + when + ' \u00b7 ' + confidence;
        },

        startEditFact(fact) {
            this.editingId = fact.id;
            this.editValue = fact.value;
        },

        async saveEditFact(id) {
            try {
                const resp = await AM.fetch('/plugins/memory/facts/' + id, {
                    method: 'PUT',
                    body: { value: this.editValue },
                });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to update fact', 'error');
                    return;
                }
                this.editingId = null;
                await this.loadFacts();
                AM.toast('Fact updated', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        deleteFact(fact) {
            const html = `
                <div class="settings-modal" style="width: 400px;">
                    <h3>Remove fact</h3>
                    <p class="memory-confirm-text">Remove "${AM.utils.escapeHtml(fact.value)}"?</p>
                    <div class="memory-confirm-actions">
                        <button class="fact-action-btn" id="memory-delete-cancel">Cancel</button>
                        <button class="fact-action-btn danger" id="memory-delete-confirm">Remove</button>
                    </div>
                </div>`;
            const m = AM.modal(html);
            document.getElementById('memory-delete-cancel').onclick = () => m.close();
            document.getElementById('memory-delete-confirm').onclick = async () => {
                m.close();
                try {
                    const resp = await AM.fetch('/plugins/memory/facts/' + fact.id, { method: 'DELETE' });
                    if (!resp || !resp.ok) {
                        AM.toast('Failed to remove fact', 'error');
                        return;
                    }
                    await this.loadFacts();
                    AM.toast('Fact removed', 'success');
                } catch (e) { AM.toast(e.message, 'error'); }
            };
        },

        categoriseFact(key) {
            const k = (key || '').toLowerCase();
            if (/name|friend|family|parent|sibling|partner|spouse|colleague|boss|coworker/i.test(k)) return 'People';
            if (/work|job|career|project|task|deadline|meeting|office|code|programming/i.test(k)) return 'Work';
            if (/like|prefer|favorite|enjoy|hobby|interest|music|food|book|movie|game|hate|dislike/i.test(k)) return 'Preferences';
            if (/health|exercise|sleep|diet|weight|doctor|medicine|therapy|wellness|gym/i.test(k)) return 'Health';
            return 'Preferences';
        },
    };
}
