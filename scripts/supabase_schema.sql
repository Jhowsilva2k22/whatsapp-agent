-- WHATSAPP AI AGENT - Schema Supabase
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS owners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone TEXT NOT NULL UNIQUE,
    business_name TEXT NOT NULL,
    business_type TEXT, notify_phone TEXT, evolution_instance TEXT,
    tone TEXT, vocabulary JSONB DEFAULT '[]', emoji_style TEXT,
    avg_response_length TEXT, values JSONB DEFAULT '[]',
    product_description TEXT, main_offer TEXT, price_range TEXT,
    target_audience TEXT, common_objections JSONB DEFAULT '[]',
    faqs JSONB DEFAULT '[]', context_summary TEXT,
    links_processed JSONB DEFAULT '[]', agent_mode TEXT DEFAULT 'both',
    qualification_questions JSONB DEFAULT '[]', handoff_threshold INT DEFAULT 70,
    daily_summary_time TEXT DEFAULT '20:00',
    created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone TEXT NOT NULL, owner_id UUID REFERENCES owners(id) ON DELETE CASCADE,
    name TEXT, communication_style TEXT, emoji_usage TEXT, avg_message_length TEXT,
    lead_score INT DEFAULT 0, lead_status TEXT DEFAULT 'novo',
    intent TEXT, last_intent TEXT, summary TEXT,
    objections JSONB DEFAULT '[]', interests JSONB DEFAULT '[]',
    total_messages INT DEFAULT 0,
    first_contact TIMESTAMPTZ DEFAULT NOW(), last_contact TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE(phone, owner_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone TEXT NOT NULL, owner_id UUID REFERENCES owners(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL,
    intent_detected TEXT, lead_score_delta INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_phone_owner ON messages(phone, owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS learnings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID REFERENCES owners(id) ON DELETE CASCADE,
    date DATE NOT NULL, data JSONB,
    hot_leads_count INT DEFAULT 0, total_conversations INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Banco de Conhecimento do Atendente ───────────────────────────────────────
-- Cada item é uma unidade de conhecimento treinada pelo dono ou extraída
-- automaticamente das conversas. O atendente consulta antes de responder.
CREATE TABLE IF NOT EXISTS knowledge_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID REFERENCES owners(id) ON DELETE CASCADE,
    -- Categoria: produto | faq | objecao | estilo | expertise | concorrente | depoimento | processo | aprendizado
    category TEXT NOT NULL DEFAULT 'faq',
    -- Conteúdo da informação (máx ~500 chars por item — autocontido)
    content TEXT NOT NULL,
    -- Origem: owner_whatsapp | nightly_learning | url_ingestao | batch
    source TEXT DEFAULT 'manual',
    -- Confiança de 0.0 a 1.0 (1.0 = direto do dono, 0.75 = aprendizado automático)
    confidence FLOAT DEFAULT 1.0,
    -- Quantas vezes o atendente usou essa informação
    times_used INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_owner_category
    ON knowledge_items(owner_id, category);

CREATE INDEX IF NOT EXISTS idx_knowledge_owner_confidence
    ON knowledge_items(owner_id, confidence DESC);

-- RPC para incrementar o contador de uso de forma atômica
CREATE OR REPLACE FUNCTION increment_knowledge_usage(item_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE knowledge_items
    SET times_used = times_used + 1
    WHERE id = item_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_updated_at() RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER owners_updated_at BEFORE UPDATE ON owners FOR EACH ROW EXECUTE FUNCTION update_updated_at();
