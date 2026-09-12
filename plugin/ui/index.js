function memoryPlugin() {
    return {
        facts: [],
        people: [],
        filter: 'All',
        editingId: null,
        editValue: '',

        async init() {
            await Promise.all([this.loadFacts(), this.loadPeople()]);
            AM.onCleanup(() => {});
        },

        async loadFacts() {
            try {
                const resp = await AM.fetch('/plugins/memory/facts');
                if (resp) this.facts = await resp.json();
            } catch (e) { console.error('Memory loadFacts', e); }
        },

        async loadPeople() {
            try {
                const resp = await AM.fetch('/plugins/memory/people');
                if (resp) this.people = await resp.json();
            } catch (e) { console.error('Memory loadPeople', e); }
        },

        async savePersonHint(person) {
            try {
                await AM.fetch('/plugins/memory/people/' + encodeURIComponent(person.name), {
                    method: 'PUT',
                    body: { pronunciation_hint: person.pronunciation_hint || '' },
                });
                await this.loadPeople();
                AM.toast('Pronunciation hint saved', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        filteredFacts() {
            if (this.filter === 'All') return this.facts;
            return this.facts.filter(f => this.categoriseFact(f.key) === this.filter);
        },

        factSource(fact) {
            const cat = this.categoriseFact(fact.key);
            return 'From ' + cat.toLowerCase() + ' \u00b7 ' + AM.utils.formatDate(fact.created_at);
        },

        startEditFact(fact) {
            this.editingId = fact.id;
            this.editValue = fact.value;
        },

        async saveEditFact(id) {
            try {
                await AM.fetch('/plugins/memory/facts/' + id, {
                    method: 'PUT',
                    body: { value: this.editValue },
                });
                this.editingId = null;
                await this.loadFacts();
                AM.toast('Fact updated', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async deleteFact(id) {
            if (!confirm('Remove this fact?')) return;
            try {
                await AM.fetch('/plugins/memory/facts/' + id, { method: 'DELETE' });
                await this.loadFacts();
                AM.toast('Fact removed', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
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
