          </div>
        </div>
        <div className="status-cluster">
          {dataSource === 'static' && <span className="snapshot-badge">Static snapshot</span>}
          {latest?.artifacts.paper_loop && (
            <span className="loop-badge">
              <span className="loop-dot" />
            <button
              key={mode}
              className={mode === 'network' ? 'run-button primary' : 'run-button'}
              disabled={isRunning || !apiAvailable}
              title={
                apiAvailable
                  ? `Run ${mode} paper measurement`
                  : 'Configure VITE_API_BASE to enable on-demand runs'
              }
              onClick={() => void startRun(mode)}
            >
              <span className="play-icon">▶</span>
