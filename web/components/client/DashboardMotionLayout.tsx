"use client";
/**
 * components/client/DashboardMotionLayout.tsx
 *
 * Wraps all KPI cards and sections with Framer Motion staggered fade-up.
 * This thin client wrapper allows the parent page.tsx to remain a Server Component.
 */

import { motion } from "framer-motion";

// ─── Stagger container ────────────────────────────────────────────────────────
const containerVariants = {
  hidden: {},
  visible: {
    transition: {
      staggerChildren: 0.09,
    },
  },
};

// ─── Each card/section ───────────────────────────────────────────────────────
const itemVariants = {
  hidden:  { opacity: 0, y: 18 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.5, ease: [0.22, 1, 0.36, 1] },
  },
};

// ─── Exports ─────────────────────────────────────────────────────────────────

/** Wraps the top-level grid that should stagger its children. */
export function MotionGrid({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <motion.div
      variants={containerVariants}
      initial="hidden"
      animate="visible"
      style={style}
    >
      {children}
    </motion.div>
  );
}

/** Each card/section that fades up. */
export function MotionCard({
  children,
  style,
  className,
}: {
  children: React.ReactNode;
  style?: React.CSSProperties;
  className?: string;
}) {
  return (
    <motion.div
      variants={itemVariants}
      className={className}
      style={style}
    >
      {children}
    </motion.div>
  );
}
