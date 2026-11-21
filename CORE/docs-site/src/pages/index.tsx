import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--primary', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">
          Build file-processing pipelines on a canvas, run them on powerful
          machines, get results — no servers or GPUs to manage.
        </p>
        <div className={styles.buttons}>
          <Link
            className="button button--primary button--lg"
            style={{margin: '0.4rem'}}
            to="/getting-started">
            Get started in 5 min
          </Link>
          <Link
            className="button button--secondary button--lg"
            style={{margin: '0.4rem'}}
            to="/how-mod-works">
            How MOD works
          </Link>
          <Link
            className="button button--secondary button--lg"
            style={{margin: '0.4rem'}}
            to="/api-reference">
            API Reference
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={`${siteConfig.title} Docs`}
      description="MOD Platform documentation — build and run file-processing workflows. Getting started, guides, and a full API reference.">
      <HomepageHeader />
      <main className="container" style={{padding: '2.5rem 1rem 3rem'}}>
        <div className="row">
          <div className="col col--4" style={{marginBottom: '1.5rem'}}>
            <Heading as="h3">🚀 New here?</Heading>
            <p>
              Start with <Link to="/how-mod-works">How MOD works</Link> for the
              2-minute mental model, then do the{' '}
              <Link to="/first-workflow">first-workflow tutorial</Link>.
            </p>
          </div>
          <div className="col col--4" style={{marginBottom: '1.5rem'}}>
            <Heading as="h3">🧩 Build something</Heading>
            <p>
              Browse the <Link to="/node-library">Node Library</Link> (130+
              nodes), wire them up in the{' '}
              <Link to="/workflow-builder">Workflow Builder</Link>, and automate
              runs with <Link to="/triggers">Triggers</Link>.
            </p>
          </div>
          <div className="col col--4" style={{marginBottom: '1.5rem'}}>
            <Heading as="h3">⚙️ Integrate</Heading>
            <p>
              The <Link to="/api-reference">REST API</Link> exposes everything,
              with a live "try it" explorer. Manage your team and settings in{' '}
              <Link to="/account-and-teams">Account &amp; Teams</Link>.
            </p>
          </div>
        </div>
      </main>
    </Layout>
  );
}
