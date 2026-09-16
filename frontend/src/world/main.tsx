import React from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "../context/ThemeContext";
import { WorldApp } from "./WorldApp";
import "../styles.css";
import "./world.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <WorldApp />
    </ThemeProvider>
  </React.StrictMode>,
);
