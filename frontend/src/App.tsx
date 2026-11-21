import {type ComponentProps, useEffect, useState} from 'react';
import {LabApp} from './Lab';
import {LegacyDashboard} from './LegacyDashboard';
import {MvpApp} from './MvpApp';

/**
 * Chooses which front end to show by asking the backend what it is.
 *
 * - `autofund lab`         reports `product: "lab"`            -> the paper-trading lab
 * - `autofund demo / app`  serve the MVP snapshot              -> the MVP application
 * - `autofund dashboard`   serves neither                      -> the shadow dashboard
 */
type Product = ComponentProps<typeof MvpApp>['initial'] | null | undefined;

export function App() {
  const [product, setProduct] = useState<Product>(undefined);
  const [lab, setLab] = useState(false);

  useEffect(() => {
    fetch('/api/v1/health')
      .then((response) => response.json())
      .then((health) => {
        if (health.product === 'lab') {
          setLab(true);
          return null;
        }
        return health.product_version ? fetch('/api/v1/mvp').then((response) => response.json()) : null;
      })
      .catch(() => null)
      .then((snapshot) => setProduct(snapshot?.schema_version === 'autofund.mvp.v1' ? snapshot : null));
  }, []);

  if (lab) return <LabApp />;
  if (product === undefined) return <main>Loading AutoFund</main>;
  return product ? <MvpApp initial={product} /> : <LegacyDashboard />;
}
