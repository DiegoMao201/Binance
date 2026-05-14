"use client";
/**
 * components/client/SupportWidget.tsx
 *
 * Floating Action Button (FAB) in the bottom-right corner.
 * Opens a Framer Motion modal with a support message form.
 * Submits via the sendSupportMessage Server Action.
 */

import { useState, useTransition, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MessageCircle, X, Send, CheckCircle, AlertCircle, ChevronRight } from "lucide-react";
import { sendSupportMessage } from "@/app/actions/support";

// ─── Tokens ──────────────────────────────────────────────────────────────────
const BG    = "#080e16";
const CARD  = "#0a1018";
const BORD  = "#1a2b3c";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#10b981";

// ─── Main component ──────────────────────────────────────────────────────────
export function SupportWidget() {
  const [open, setOpen]         = useState(false);
  const [subject, setSubject]   = useState("");
  const [message, setMessage]   = useState("");
  const [status, setStatus]     = useState<"idle" | "ok" | "err">("idle");
  const [errMsg, setErrMsg]     = useState("");
  const [isPending, startTransition] = useTransition();
  const subjectRef = useRef<HTMLInputElement>(null);

  // Focus subject field when modal opens
  useEffect(() => {
    if (open) setTimeout(() => subjectRef.current?.focus(), 120);
  }, [open]);

  function handleClose() {
    setOpen(false);
    // Reset form after exit animation completes
    setTimeout(() => {
      setSubject("");
      setMessage("");
      setStatus("idle");
      setErrMsg("");
    }, 300);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!subject.trim() || !message.trim()) return;
    startTransition(async () => {
      const result = await sendSupportMessage(subject, message);
      if (result.success) {
        setStatus("ok");
        setTimeout(handleClose, 2000);
      } else {
        setStatus("err");
        setErrMsg(result.error ?? "Error desconocido");
      }
    });
  }

  return (
    <>
      {/* ── Backdrop ── */}
      <AnimatePresence>
        {open && (
          <motion.div
            key="backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={handleClose}
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(4,8,15,0.75)",
              backdropFilter: "blur(4px)",
              zIndex: 49,
            }}
          />
        )}
      </AnimatePresence>

      {/* ── Modal ── */}
      <AnimatePresence>
        {open && (
          <motion.div
            key="modal"
            initial={{ opacity: 0, y: 28, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
            style={{
              position: "fixed",
              bottom: 90,
              right: 24,
              width: "min(400px, calc(100vw - 32px))",
              background: CARD,
              border: `1px solid ${BORD}`,
              borderRadius: 20,
              zIndex: 50,
              overflow: "hidden",
              boxShadow: "0 24px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.04)",
            }}
          >
            {/* Header */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "18px 20px 16px",
                borderBottom: `1px solid ${BORD}`,
                background: "linear-gradient(135deg, #0d1624 0%, #0a1018 100%)",
              }}
            >
              <div>
                <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 2 }}>
                  Centro de Soporte
                </p>
                <p style={{ color: TEXT, fontSize: 15, fontWeight: 700 }}>
                  Contactar al Equipo
                </p>
              </div>
              <button
                onClick={handleClose}
                style={{
                  background: "rgba(255,255,255,0.05)",
                  border: `1px solid ${BORD}`,
                  borderRadius: 8,
                  color: MUTE,
                  cursor: "pointer",
                  padding: 6,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  transition: "background 0.15s",
                }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.09)")}
                onMouseLeave={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.05)")}
              >
                <X size={16} />
              </button>
            </div>

            {/* Body */}
            <AnimatePresence mode="wait">
              {status === "ok" ? (
                <motion.div
                  key="success"
                  initial={{ opacity: 0, scale: 0.95 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0 }}
                  style={{
                    padding: "40px 20px",
                    textAlign: "center",
                  }}
                >
                  <motion.div
                    initial={{ scale: 0.5, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    transition={{ type: "spring", stiffness: 260, damping: 18 }}
                    style={{ marginBottom: 16 }}
                  >
                    <CheckCircle size={40} color={GREEN} style={{ margin: "0 auto" }} />
                  </motion.div>
                  <p style={{ color: TEXT, fontWeight: 700, fontSize: 16, marginBottom: 6 }}>
                    Mensaje enviado
                  </p>
                  <p style={{ color: MUTE, fontSize: 13 }}>
                    Te responderemos a la brevedad posible.
                  </p>
                </motion.div>
              ) : (
                <motion.form
                  key="form"
                  onSubmit={handleSubmit}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  style={{ padding: "20px" }}
                >
                  {/* Error banner */}
                  <AnimatePresence>
                    {status === "err" && (
                      <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          background: "rgba(235,75,97,0.1)",
                          border: "1px solid rgba(235,75,97,0.25)",
                          borderRadius: 10,
                          padding: "10px 14px",
                          marginBottom: 16,
                          color: "#eb4b61",
                          fontSize: 12,
                          overflow: "hidden",
                        }}
                      >
                        <AlertCircle size={14} />
                        {errMsg}
                      </motion.div>
                    )}
                  </AnimatePresence>

                  {/* Subject */}
                  <div style={{ marginBottom: 14 }}>
                    <label style={{ display: "block", color: MUTE, fontSize: 11, fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
                      Asunto
                    </label>
                    <input
                      ref={subjectRef}
                      type="text"
                      value={subject}
                      onChange={(e) => { setSubject(e.target.value); setStatus("idle"); }}
                      placeholder="Ej: Consulta sobre mi balance"
                      maxLength={120}
                      required
                      style={{
                        width: "100%",
                        background: BG,
                        border: `1px solid ${BORD}`,
                        borderRadius: 10,
                        padding: "10px 14px",
                        color: TEXT,
                        fontSize: 13,
                        outline: "none",
                        boxSizing: "border-box",
                        transition: "border-color 0.15s",
                      }}
                      onFocus={(e) => (e.currentTarget.style.borderColor = "rgba(99,102,241,0.5)")}
                      onBlur={(e) => (e.currentTarget.style.borderColor = BORD)}
                    />
                  </div>

                  {/* Message */}
                  <div style={{ marginBottom: 18 }}>
                    <label style={{ display: "block", color: MUTE, fontSize: 11, fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
                      Mensaje
                    </label>
                    <textarea
                      value={message}
                      onChange={(e) => { setMessage(e.target.value); setStatus("idle"); }}
                      placeholder="Describe tu consulta o inquietud..."
                      maxLength={2000}
                      rows={4}
                      required
                      style={{
                        width: "100%",
                        background: BG,
                        border: `1px solid ${BORD}`,
                        borderRadius: 10,
                        padding: "10px 14px",
                        color: TEXT,
                        fontSize: 13,
                        outline: "none",
                        resize: "none",
                        boxSizing: "border-box",
                        fontFamily: "inherit",
                        transition: "border-color 0.15s",
                      }}
                      onFocus={(e) => (e.currentTarget.style.borderColor = "rgba(99,102,241,0.5)")}
                      onBlur={(e) => (e.currentTarget.style.borderColor = BORD)}
                    />
                    <p style={{ color: MUTE, fontSize: 10, textAlign: "right", marginTop: 4 }}>
                      {message.length}/2000
                    </p>
                  </div>

                  {/* Submit */}
                  <button
                    type="submit"
                    disabled={isPending || !subject.trim() || !message.trim()}
                    style={{
                      width: "100%",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 8,
                      background: isPending
                        ? "rgba(99,102,241,0.3)"
                        : "linear-gradient(135deg, #4f46e5 0%, #6366f1 100%)",
                      border: "none",
                      borderRadius: 12,
                      color: "#fff",
                      fontSize: 13,
                      fontWeight: 700,
                      padding: "12px 20px",
                      cursor: isPending ? "default" : "pointer",
                      transition: "opacity 0.2s",
                      opacity: !subject.trim() || !message.trim() ? 0.45 : 1,
                      letterSpacing: "0.02em",
                    }}
                  >
                    {isPending ? (
                      <>
                        <motion.span
                          animate={{ rotate: 360 }}
                          transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }}
                          style={{ display: "inline-block", width: 14, height: 14, border: "2px solid rgba(255,255,255,0.3)", borderTop: "2px solid #fff", borderRadius: "50%" }}
                        />
                        Enviando...
                      </>
                    ) : (
                      <>
                        <Send size={14} />
                        Enviar mensaje
                        <ChevronRight size={14} />
                      </>
                    )}
                  </button>
                </motion.form>
              )}
            </AnimatePresence>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── FAB ── */}
      <motion.button
        onClick={() => setOpen((v) => !v)}
        title="Soporte"
        whileHover={{ scale: 1.08 }}
        whileTap={{ scale: 0.94 }}
        style={{
          position: "fixed",
          bottom: 24,
          right: 24,
          width: 52,
          height: 52,
          borderRadius: "50%",
          background: "linear-gradient(135deg, #4f46e5 0%, #6366f1 100%)",
          border: "none",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 48,
          boxShadow: "0 4px 24px rgba(99,102,241,0.35), 0 0 0 1px rgba(99,102,241,0.2)",
          color: "#fff",
        }}
        aria-label="Abrir soporte"
      >
        <AnimatePresence mode="wait" initial={false}>
          {open ? (
            <motion.span
              key="close"
              initial={{ rotate: -90, opacity: 0 }}
              animate={{ rotate: 0, opacity: 1 }}
              exit={{ rotate: 90, opacity: 0 }}
              transition={{ duration: 0.18 }}
              style={{ display: "flex" }}
            >
              <X size={20} />
            </motion.span>
          ) : (
            <motion.span
              key="open"
              initial={{ rotate: 90, opacity: 0 }}
              animate={{ rotate: 0, opacity: 1 }}
              exit={{ rotate: -90, opacity: 0 }}
              transition={{ duration: 0.18 }}
              style={{ display: "flex" }}
            >
              <MessageCircle size={20} />
            </motion.span>
          )}
        </AnimatePresence>
      </motion.button>
    </>
  );
}
