# Migração de nomes — extensões e integrações Hermes

**Status:** origens das extensões consolidadas; aguardando reconciliação do runtime `delegate-profile` antes da Fase A
**Escopo:** `delegate-profile`, extensões do Hermes One e seus sidecars locais.
**Fora de escopo:** renomear profiles Hermes, tool names já expostos ou alterar
portas/hosts de serviços.

## 0. Origens Git consolidadas

| Componente | Origem | Commit inicial publicado | Estado |
|---|---|---|---|
| Bundle de extensões Hermes One | `https://github.com/rodrigogs/hermes-one-extensions` | `f2866eb` | `main` rastreia `origin/main` |
| Fact Explorer | `https://github.com/rodrigogs/hermes-one-fact-explorer` | `aa0ee35` | `main` rastreia `origin/main` |
| Router/delegação | `https://github.com/rodrigogs/hermes-delegate-profile` | existente | runtime ainda precisa ser reconciliado |

Os dois primeiros repositórios foram publicados após snapshot local, revisão de
diff, scan de credenciais de alta confiança e execução das suítes focadas. O
antigo `assistant-avatar`, que já não constava no manifest nem tinha referências
ativas, foi removido explicitamente do bundle.

## 1. Princípios

1. O ID precisa nomear uma responsabilidade, não um detalhe acidental da
   implementação.
2. IDs de extensões Hermes One usam o prefixo `hermes-one-`: eles coexistem no
   manifest local, podem aparecer em URLs de proxy e não devem competir por um
   nome genérico com projetos de terceiros.
3. Labels para pessoas são curtos; IDs para máquinas são explícitos.
4. Um ID de extensão que tem sidecar é um contrato: identifica manifest,
   diretório, URL `/api/extensions/<id>/sidecar`, estado de consentimento e
   token `token-v1`.
5. Cada migração deve manter o runtime saudável por si só. Não agrupar mudanças
   de sidecar com mudanças puramente visuais.

## 2. Identidades finais

| Camada | Atual | Final | Label humano | Decisão |
|---|---|---|---|---|
| Plugin Agent | `delegate-profile` | `delegate-profile` | Delegate Profile | **manter** |
| Tool Agent | `delegate_profile` | `delegate_profile` | — | **manter** |
| Biblioteca de extensões One | `hermes-one-extension-kit` | `hermes-one-extension-kit` | Hermes One Extension Kit | **concluído** |
| Painel Office | `hermes-one-office-3d` | `hermes-one-office-3d` | Office 3D | **concluído** |
| Painel/sidecar de roteamento | `capability-router` | `hermes-one-profile-router` | Profile Router | renomear |
| Painel/sidecar de memória | `memory-graph` | `hermes-one-fact-explorer` | Fact Explorer | renomear |
| Unit Router | `hermes-router-sidecar.service` | manter inicialmente | Profile Router sidecar | label agora; ID depois |
| Unit Memory | `hermes-memory-sidecar.service` | manter inicialmente | Fact Explorer sidecar | label agora; ID depois |

### Justificativas

- `delegate-profile` já é um nome preciso para a API que cria uma fronteira de
  processo entre profiles. Renomeá-lo quebraria o tool name, configuração,
  estado e distribuição sem corrigir ambiguidade material.
- `hermes-panel` não é um painel: fornece navegação, ciclo de vida de views e
  bridge de tema para as demais extensões. `hermes-one-extension-kit` descreve
  essa função e não disputa o nome genérico `hermes-panel`.
- O Office não é mais um launcher separado; é uma view persistente no shell.
- O router decide o **profile** alvo e só então delega. Ele não é um framework
  genérico de capability routing. `Profile Router` é a superfície correta.
- A memória apresenta fatos, confiança e alcance; o grafo é somente uma das
  visualizações. `Fact Explorer` descreve a experiência real.

## 3. Compatibilidade que cada ID exige

| Contrato | Kit | Office | Profile Router | Fact Explorer |
|---|---:|---:|---:|---:|
| `extensions.json` | sim | sim | sim | sim |
| diretório de assets | sim | sim | sim | sim |
| referências JavaScript/CSS | sim | sim | sim | sim |
| ordem de carregamento | sim | sim | sim | sim |
| URL sidecar | não | não | sim | sim |
| consentimento persistido | não | não | sim | sim |
| token `token-v1` | não | não | sim | sim |
| instalador/updater | sim | sim | sim | sim |
| unidade systemd | não | não | label; ID posterior | label; ID posterior |

## 4. Ordem de execução

### Fase A — Extension Kit

Renomear o diretório e ID `hermes-panel` para `hermes-one-extension-kit` de
forma atômica no manifest e nos três consumidores. É a menor mudança de risco:
não possui sidecar, token nem estado de consentimento.

**Aceite:** todos os três painéis continuam montando, a navegação nativa do
Hermes One continua coerente em desktop/mobile e os testes JavaScript passam.

### Fase B — Office 3D

Concluída. `office-3d-launcher` renomeado para `hermes-one-office-3d`; a
funcionalidade e a URL `/office/` não mudaram. A classe de navegação acompanhou o
ID (`office-3d-nav` para `hermes-one-office-3d-nav`), seguindo a convenção dos
irmãos, e `REQUIRED_EXTENSION_IDS` no updater passou a nomear os IDs pós-rename —
ele ainda exigia `hermes-panel` e falharia sobre um manifest saudável.

**Aceite:** o Office abre no painel, preserva iframe/câmera ao alternar de
view e não recarrega ao retornar.

### Fase C — Profile Router

Renomear `capability-router` para `hermes-one-profile-router` em manifest,
assets, instalador, sidecar, testes e updater. Criar uma migração explícita de
consentimento/token ou exigir novo consentimento de forma visível — nunca
silenciar uma falha 403.

A unit fica com o nome atual na primeira release, mas recebe a descrição
`Hermes One Profile Router sidecar`. A renomeação da unit é uma operação
separada com alias e rollback.

**Aceite:** status de extensão, proxy consentido, `/health`, console do Router
e `delegate_profile` funcionam depois da migração.

### Fase D — Fact Explorer e origem versionada

Primeiro mover o código que hoje está em `~/.hermes/plugins/memory-graph` para
uma origem Git versionada e rastreável; ele não é um plugin Agent porque não
possui `plugin.yaml`. Só então renomear a extensão para
`hermes-one-fact-explorer` e atualizar o manifest/sidecar.

A unit mantém o ID de arquivo atual nesta fase e passa a ter descrição
`Hermes One Fact Explorer sidecar`.

**Aceite:** console read-only abre por proxy, facts/lista/pesquisa/grafo
funcionam, não há escrita no store e o updater inclui a nova origem.

## 5. Reversão

Cada fase produz um commit independente. Em caso de falha, reverter apenas o
commit da fase correspondente e reiniciar somente os serviços afetados. As
fases A e B não exigem restart de sidecar; C e D exigem healthcheck antes de
considerar a troca concluída.

## 6. Pesquisa de colisões

As buscas públicas em 2026-07-27 encontraram colisões relevantes para os nomes
atuais `hermes-panel`, `capability-router` e `memory-graph`. Não retornaram
repositórios GitHub para os IDs finais acima no momento da consulta. Isso não
constitui registro de marca; é uma verificação prática de namespace e
confusão operacional.
