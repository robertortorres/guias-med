// Tela de revisão do lote: progresso, edição dos pacotes e geração do ZIP.
(() => {
  const ID = window.LOTE_ID;
  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
    }
    for (const k of kids.flat()) if (k != null) e.append(k);
    return e;
  };

  let lote = null;
  let pacotes = [];
  let saveTimer = null;

  // ------------------------------------------------------------- nome do arquivo (mesma regra do servidor)
  const semAcento = (s) => s.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
  const limpaNome = (s) => semAcento(s).toUpperCase().replace(/[^A-Z ]/g, ' ')
    .split(/\s+/).filter((w) => w.length > 1 || w === 'E').join(' ');
  const titulo = (w) => w.charAt(0) + w.slice(1).toLowerCase();
  const nomeCurto = (full) => {
    const p = limpaNome(full).split(' ').filter(Boolean);
    if (!p.length) return '';
    return p.length === 1 ? titulo(p[0]) : `${titulo(p[0])} ${titulo(p[p.length - 1])}`;
  };
  const nomeArquivo = (p) => {
    const padrao = lote.nomes_arquivo[p.tipo] || '{numero} - {nome}';
    return padrao.replace('{numero}', p.numero || '?').replace('{nome}', nomeCurto(p.nome) || '?') + '.pdf';
  };

  // ------------------------------------------------------------- alertas calculados na tela
  function alertasDe(p, contagem) {
    const a = [...(p.alertas || [])];
    if (!p.numero) a.unshift('Número da guia não lido. Digite olhando o recorte.');
    if (!p.nome) a.unshift('Nome não lido. Digite olhando o recorte.');
    if (p.numero && contagem[p.numero] > 1) a.unshift('Número repetido neste lote.');
    return a;
  }
  const contar = () => pacotes.reduce((c, p) => (p.numero && (c[p.numero] = (c[p.numero] || 0) + 1), c), {});

  // ------------------------------------------------------------- carregamento / progresso
  async function carregar() {
    const r = await fetch(`/lotes/${ID}/dados`);
    if (!r.ok) { mostraErro('Não foi possível abrir o lote. Ele pode ter sido apagado pela limpeza automática.'); return; }
    lote = await r.json();
    if (lote.status === 'processando') {
      $('#progresso').hidden = false;
      const pct = lote.total ? Math.round((lote.feitos / lote.total) * 100) : 0;
      $('#progresso-texto').textContent = `Lendo página ${lote.feitos} de ${lote.total}`;
      $('#progresso-barra').style.width = `${pct}%`;
      setTimeout(carregar, 1500);
      return;
    }
    $('#progresso').hidden = true;
    if (lote.status === 'erro') { mostraErro(`O processamento falhou: ${lote.erro}`); return; }
    pacotes = lote.pacotes.map((p) => ({ ...p }));
    $('#barra').hidden = false;
    $('#revisao').hidden = false;
    render();
  }

  function mostraErro(msg) {
    const e = $('#erro');
    e.hidden = false;
    e.textContent = msg;
  }

  // ------------------------------------------------------------- render
  const img = (nome, alt, cls) => nome ? el('img', { src: `/lotes/${ID}/img/${encodeURIComponent(nome)}`, alt, class: cls, loading: 'lazy' }) : null;

  function campo(p, chave, rotulo, crop, pagina) {
    const input = el('input', {
      value: p[chave] || '', 'aria-label': rotulo, class: 'campo-input', 'data-campo': chave,
      placeholder: chave === 'numero' ? 'Digite o nº da guia' : 'Digite o nome como está no recorte',
      inputmode: chave === 'numero' ? 'numeric' : 'text', autocomplete: 'off', spellcheck: 'false',
      oninput: (ev) => { p[chave] = ev.target.value; ev.target.classList.toggle('vazio', !ev.target.value.trim()); atualizaLinha(p); agendaSalvar(); },
      onkeydown: (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); proximoCampo(ev.target); } },
    });
    let fonte = null;
    if (chave === 'numero' && p.numero_fonte === 'barcode') fonte = el('span', { class: 'fonte ok' }, 'código de barras');
    if (chave === 'nome' && p.nome_fonte === 'nome_social') fonte = el('span', { class: 'fonte atencao' }, 'nome social');
    if (!input.value.trim()) input.classList.add('vazio');
    const recorte = crop
      ? el('button', {
          class: 'recorte', type: 'button', title: 'Clique para ampliar',
          'aria-label': `Ampliar recorte: ${rotulo}`,
          onclick: () => abrirImagem(crop, `Recorte: ${rotulo}`, pagina),
        }, img(crop, `Recorte da guia: ${rotulo}`), el('span', { class: 'lupa' }, 'Ampliar'))
      : el('span', { class: 'sem-recorte' }, 'sem recorte');
    return el('div', { class: `campo campo-${chave}` },
      el('span', { class: 'campo-rotulo' }, rotulo, fonte),
      recorte,
      input);
  }

  function linha(p, idx) {
    const paginas = el('div', { class: 'paginas' });
    p.paginas.forEach((pg, j) => {
      if (j > 0) {
        paginas.append(el('button', {
          class: 'dividir', title: 'Dividir aqui: esta página começa um novo pacote',
          'aria-label': `Dividir o pacote ${idx + 1} antes da página ${j + 1}`,
          onclick: () => dividir(idx, j),
        }, 'Dividir aqui'));
      }
      const info = lote.paginas[pg];
      paginas.append(el('button', { class: 'miniatura', onclick: () => abrir(info), 'aria-label': `Ampliar página ${j + 1}` },
        img(info.thumb, `Página ${info.pagina + 1} de ${info.arquivo}`),
        el('span', { class: 'mini-legenda' }, (info.tipo === 'consulta' || info.tipo === 'sadt') ? 'guia' : 'anexo')));
    });

    const tipo = el('select', {
      class: 'tipo', 'aria-label': 'Tipo de guia',
      onchange: (ev) => { p.tipo = ev.target.value; atualizaLinha(p); agendaSalvar(); },
    }, el('option', { value: 'consulta' }, 'Guia de consulta'), el('option', { value: 'sadt' }, 'SP/SADT (pedido médico)'));
    tipo.value = p.tipo;

    const acoes = el('div', { class: 'acoes' },
      idx > 0 ? el('button', { class: 'botao discreto', onclick: () => juntar(idx) }, 'Juntar com o anterior') : null);

    const li = el('li', { class: 'pacote', 'data-id': p.id },
      el('div', { class: 'ordem' }, String(idx + 1)),
      paginas,
      el('div', { class: 'campos' },
        campo(p, 'numero', 'Nº da guia', p.crop_numero, lote.paginas[p.paginas[0]]),
        campo(p, 'nome', 'Nome do beneficiário', p.crop_nome, lote.paginas[p.paginas[0]]),
        el('div', { class: 'meta' }, p.convenio ? el('span', { class: 'convenio' }, p.convenio) : null, tipo)),
      el('div', { class: 'saida' },
        el('span', { class: 'saida-rotulo' }, 'Arquivo'),
        el('p', { class: 'arquivo' }),
        el('ul', { class: 'alertas' }),
        acoes));
    p._li = li;
    atualizaLinha(p);
    return li;
  }

  function atualizaLinha(p) {
    const li = p._li;
    if (!li) return;
    li.querySelector('.arquivo').textContent = nomeArquivo(p);
    const contagem = contar();
    const lista = li.querySelector('.alertas');
    lista.replaceChildren(...alertasDe(p, contagem).map((a) => {
      const alvo = a.startsWith('Nome') || a.includes('leituras do nome') ? 'nome' : a.startsWith('Número da guia') ? 'numero' : null;
      if (!alvo) return el('li', {}, a);
      return el('li', {}, el('button', { class: 'alerta-link', type: 'button', onclick: () => focaCampo(p, alvo) }, a));
    }));
    li.classList.toggle('com-alerta', lista.children.length > 0);
    resumo();
  }

  function render() {
    const ol = $('#pacotes');
    ol.replaceChildren(...pacotes.map((p, i) => linha(p, i)));
    pacotes.forEach(atualizaLinha);
    filtrar();
  }

  function resumo() {
    const contagem = contar();
    const alertas = pacotes.filter((p) => alertasDe(p, contagem).length).length;
    $('#resumo').textContent = `${pacotes.length} pacotes, ${lote.paginas.length} páginas` + (alertas ? `, ${alertas} para conferir` : ', nenhum alerta');
  }

  function filtrar() {
    const so = $('#so-alertas').checked;
    pacotes.forEach((p) => { if (p._li) p._li.hidden = so && !p._li.classList.contains('com-alerta'); });
  }

  function proximoCampo(atual) {
    const todos = [...document.querySelectorAll('.pacote:not([hidden]) .campo-input')];
    const i = todos.indexOf(atual);
    if (i >= 0 && i + 1 < todos.length) { todos[i + 1].focus(); todos[i + 1].select(); }
  }

  // ------------------------------------------------------------- reagrupamento
  function dividir(idx, j) {
    const p = pacotes[idx];
    const resto = p.paginas.splice(j);
    const info = lote.paginas[resto[0]];
    const novo = {
      id: `n${Date.now()}`, paginas: resto, tipo: info.tipo === 'sadt' ? 'sadt' : p.tipo, convenio: info.convenio || p.convenio,
      numero: info.numero || '', nome: info.nome || '', numero_fonte: info.numero_fonte, nome_fonte: info.nome_fonte,
      crop_numero: info.crop_numero, crop_nome: info.crop_nome, alertas: [...(info.alertas || [])],
    };
    pacotes.splice(idx + 1, 0, novo);
    render();
    agendaSalvar();
  }

  function juntar(idx) {
    const atual = pacotes[idx];
    pacotes[idx - 1].paginas.push(...atual.paginas);
    pacotes.splice(idx, 1);
    render();
    agendaSalvar();
  }

  // ------------------------------------------------------------- visor com zoom
  let zoom = 1;
  let paginaVisor = null;
  const vimg = $('#visor-img');

  function aplicaZoom() {
    if (!vimg.naturalWidth) return;
    vimg.style.width = `${Math.round(vimg.naturalWidth * zoom)}px`;
    $('#visor-zoom').textContent = `${Math.round(zoom * 100)}%`;
  }
  function ajustaTela() {
    const area = $('#visor-area');
    zoom = Math.min(3, (area.clientWidth - 16) / vimg.naturalWidth);
    aplicaZoom();
  }
  function abrirImagem(nome, titulo, pagina) {
    paginaVisor = pagina || null;
    $('#visor-titulo').textContent = titulo;
    $('#visor-pagina').hidden = !paginaVisor;
    vimg.onload = () => { ajustaTela(); };
    vimg.src = `/lotes/${ID}/img/${encodeURIComponent(nome)}`;
    vimg.alt = titulo;
    $('#visor').showModal();
  }
  function abrir(info) {
    abrirImagem(info.view, `Página ${info.pagina + 1} de ${info.arquivo}`, null);
  }
  $('#visor-mais').addEventListener('click', () => { zoom = Math.min(6, zoom * 1.25); aplicaZoom(); });
  $('#visor-menos').addEventListener('click', () => { zoom = Math.max(0.2, zoom / 1.25); aplicaZoom(); });
  $('#visor-ajustar').addEventListener('click', ajustaTela);
  $('#visor-pagina').addEventListener('click', () => { if (paginaVisor) abrir(paginaVisor); });
  $('#visor-area').addEventListener('wheel', (ev) => {
    if (!ev.ctrlKey) return;            // Ctrl + rolagem = zoom
    ev.preventDefault();
    zoom = ev.deltaY < 0 ? Math.min(6, zoom * 1.1) : Math.max(0.2, zoom / 1.1);
    aplicaZoom();
  }, { passive: false });
  $('#visor-fechar').addEventListener('click', () => $('#visor').close());
  $('#visor').addEventListener('click', (ev) => { if (ev.target.id === 'visor') $('#visor').close(); });

  function focaCampo(p, chave) {
    if (p._li.hidden) { $('#so-alertas').checked = false; filtrar(); }
    const input = p._li.querySelector(`.campo-input[data-campo="${chave}"]`);
    p._li.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => { input.focus(); input.select(); }, 250);
  }

  // ------------------------------------------------------------- salvar / exportar
  const limpo = () => pacotes.map(({ _li, ...p }) => p);

  function agendaSalvar() {
    $('#salvo').textContent = 'Alterações não salvas';
    clearTimeout(saveTimer);
    saveTimer = setTimeout(salvar, 800);
  }

  async function salvar() {
    const r = await fetch(`/lotes/${ID}/pacotes`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pacotes: limpo() }),
    });
    $('#salvo').textContent = r.ok ? 'Alterações salvas' : 'Não foi possível salvar; tente de novo';
  }

  $('#so-alertas').addEventListener('change', filtrar);

  $('#gerar').addEventListener('click', async () => {
    const faltando = pacotes.filter((p) => !p.numero?.trim() || !p.nome?.trim());
    if (faltando.length) {
      $('#salvo').textContent = faltando.length === 1
        ? 'Falta preencher 1 pacote; o campo já está selecionado.'
        : `Faltam ${faltando.length} pacotes; o primeiro campo vazio já está selecionado. Depois use Enter para seguir.`;
      const p = faltando[0];
      focaCampo(p, !p.numero?.trim() ? 'numero' : 'nome');
      return;
    }
    const contagem = contar();
    const repetidos = Object.keys(contagem).filter((n) => contagem[n] > 1);
    if (repetidos.length && !confirm(`Há números repetidos neste lote (${repetidos.join(', ')}). Gerar o ZIP mesmo assim?`)) return;

    const btn = $('#gerar');
    btn.disabled = true;
    btn.textContent = 'Gerando ZIP…';
    try {
      clearTimeout(saveTimer);
      const r = await fetch(`/lotes/${ID}/exportar`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pacotes: limpo() }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(e.detail || 'Não foi possível gerar o ZIP.');
        return;
      }
      const blob = await r.blob();
      const cd = r.headers.get('Content-Disposition') || '';
      const nome = (cd.match(/filename="([^"]+)"/) || [])[1] || 'guias.zip';
      const a = el('a', { href: URL.createObjectURL(blob), download: nome });
      document.body.append(a); a.click(); a.remove();
      $('#salvo').textContent = 'ZIP gerado';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Gerar ZIP';
    }
  });

  carregar();
})();
