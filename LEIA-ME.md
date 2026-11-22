# Conector "Leis do Brasil" no serverchacha

Um serviço pequeno, em Docker, que baixa todo o acervo de leis em Markdown, monta um índice de busca e conversa com o Claude pelo protocolo de conectores (MCP). Depois de ligado, em qualquer conversa o Claude consegue:

- **buscar_leis**: achar normas por assunto em cerca de 1,2 milhão de arquivos (federal, estadual, municipal, reguladores, súmulas, tratados);
- **buscar_por_numero**: achar uma norma pelo número;
- **ler_norma**: ler o texto, de preferência só o artigo ou o intervalo de artigos que interessa;
- **status_acervo**: ver o que está indexado e quando foi atualizado.

Fontes: [ctresb/leis](https://github.com/ctresb/leis) (acervo completo) e [LawCodex/br](https://github.com/LawCodex/br) (federal com texto compilado do Planalto, que cobre leis em que o ctresb tem só a republicação, como a 14.133 e a LC 214).

## O que precisa no servidor
- Docker e Docker Compose (já tem, por causa do Minecraft e do Portainer).
- Cerca de **15 GB livres**: uns 8 GB de repositórios e 2 a 3 GB de índice, com folga.
- Tailscale com Funnel liberado (passo 4).

## Passo a passo

### 1. Copiar a pasta para o servidor
No PC, na pasta onde baixou o `conector-leis.zip` (PowerShell):
```powershell
scp conector-leis.zip SEU_USUARIO@serverchacha:~/
```
No servidor:
```bash
cd ~ && unzip conector-leis.zip && cd conector-leis
```

### 2. Criar o token
```bash
cp .env.example .env
sed -i "s/^TOKEN=.*/TOKEN=$(openssl rand -hex 24)/" .env
grep TOKEN .env      # anote: ele faz parte do endereço do conector
```

### 3. Subir o serviço
```bash
docker compose up -d --build
docker logs -f leis-mcp     # Ctrl+C para sair dos logs
```
Na primeira vez, ele baixa os repositórios (de 30 min a algumas horas, conforme a internet) e depois indexa tudo (cerca de 30 a 60 min). Dá para usar o conector antes de terminar: as respostas avisam que o acervo ainda está em preparo.

Se preferir pelo Portainer: **Stacks → Add stack → Repository/Upload**, apontando para esta pasta. O mais simples é rodar o `docker compose` acima uma vez; o container aparece no Portainer do mesmo jeito.

### 4. Expor com Tailscale Funnel
O Funnel cria um endereço HTTPS público só para essa porta. O resto do servidor continua fechado.
```bash
sudo tailscale funnel status                                   # veja se a 8443 está livre
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8787
```
Se for a primeira vez usando Funnel, o comando mostra um link para liberar o recurso no painel do Tailscale. Abra, aprove e rode de novo.

O endereço do conector fica assim:
```
https://serverchacha.SEU-TAILNET.ts.net:8443/SEU_TOKEN/mcp
```
Para ver o nome completo da máquina: `tailscale status --self` ou `tailscale dns status`.

Teste (deve responder com um JSON, não com 404):
```bash
curl -s -X POST "https://serverchacha.SEU-TAILNET.ts.net:8443/SEU_TOKEN/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
```

### 5. Adicionar no Claude
Na área de conectores das configurações do Claude, adicione um **conector personalizado** com o nome "Leis do Brasil" e cole o endereço do passo 4. Não precisa de login: o token já está no endereço. Depois, ative o conector nas conversas em que quiser usar.

## Manutenção
- **Atualização:** todo dia às 04:30 ele faz `git fetch` nos dois repositórios e reindexa só o que mudou. O horário fica em `HORA_ATUALIZACAO` no `.env`.
- **Ver estado:** peça ao Claude "status do acervo de leis" ou rode `docker logs --tail 50 leis-mcp`.
- **Reindexar do zero:** `docker compose down && rm dados/indice.sqlite* && docker compose up -d`.
- **Trocar o token:** edite o `.env`, rode `docker compose up -d` e atualize o endereço no Claude.
- **Desligar o acesso externo:** `sudo tailscale funnel --https=8443 off`.

## Segurança
- O container só escuta em `127.0.0.1`; o único caminho de fora é o Funnel, e sem o token a resposta é 404.
- O conteúdo é público (leis), mas o token evita que estranhos usem o seu servidor.
- O serviço só lê os arquivos do acervo: `ler_norma` recusa qualquer caminho fora dele.
