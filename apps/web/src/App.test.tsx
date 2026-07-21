import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

beforeEach(()=>{vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('offline')))})
describe('business portal',()=>{
  it('renders the business dashboard with plain-language metrics',async()=>{render(<MemoryRouter initialEntries={['/dashboard']}><App/></MemoryRouter>);expect(screen.getByRole('heading',{name:'经营看板'})).toBeInTheDocument();await waitFor(()=>expect(screen.getByText('服务暂未连接，正在展示示例数据')).toBeInTheDocument());expect(screen.getByText('净销售额')).toBeInTheDocument();expect(screen.queryByText(/ETL|数据库|血缘|批次/)).not.toBeInTheDocument()})
  it('navigates to the three-step file experience',async()=>{const user=userEvent.setup();render(<MemoryRouter initialEntries={['/dashboard']}><App/></MemoryRouter>);await user.click(screen.getByRole('link',{name:/本月数据/}));expect(screen.getByRole('heading',{name:/准备 2026年06月经营数据/})).toBeInTheDocument();expect(screen.getByText('添加文件')).toBeInTheDocument();expect(screen.getByRole('button',{name:/选择文件/})).toBeInTheDocument()})
  it('shows at most three suggested business questions',()=>{render(<MemoryRouter initialEntries={['/ask']}><App/></MemoryRouter>);expect(document.querySelectorAll('.answer-options button').length).toBeLessThanOrEqual(3);expect(screen.getByLabelText('业务问题')).toBeInTheDocument()})
  it('renders admin data sources and primary action',async()=>{render(<MemoryRouter initialEntries={['/admin/sources']}><App/></MemoryRouter>);expect(screen.getByRole('heading',{name:'数据来源'})).toBeInTheDocument();expect(screen.getByRole('button',{name:/新增数据来源/})).toBeInTheDocument();await waitFor(()=>expect(screen.getByText(/服务未连接/)).toBeInTheDocument())})
})
